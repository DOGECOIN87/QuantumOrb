"""Offline wire-contract and journal-before-decode tests for Parsed Streams."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.base import Base
from app.db.catalog import load_pinned_catalog
from app.db.decode_outcomes import DecodeOutcomeRepository
from app.db.models import raw_events
from app.db.raw_journal import RawJournal
from app.decoders import DecodePipeline, DecoderConfigurationError, DecoderInput
from app.domain.enums import Commitment, DecodeStatus, ProgramName
from app.integrations.parsed_stream_decoder import ParsedStreamsDecoder
from app.integrations.parsed_stream_models import (
    ParsedStreamModelError,
    ProgramDescription,
    notification_envelope,
)

SETTINGS = Settings(_env_file=None, ingest_mode="demo", ingest_transport="fixture")
PUMP_ID = SETTINGS.pump_program_id
PUMP_AMM_ID = SETTINGS.pump_amm_program_id
ADDRESS = "1" * 32
SIGNATURE = "1" * 64
RECEIVED_AT = datetime(2026, 10, 13, 12, tzinfo=UTC)
REVISION = "verified-pinned-idl"


def catalog() -> dict[str, dict[str, Any]]:
    return {
        snapshot.program_id: json.loads(snapshot.payload_json)
        for snapshot in load_pinned_catalog(SETTINGS)
    }


def descriptions() -> dict[str, ProgramDescription]:
    return {
        program_id: ProgramDescription(
            id=program_id,
            name="pump" if program_id == PUMP_ID else "pump_amm",
            instructions=[item["name"] for item in payload["instructions"]],
            events=[item["name"] for item in payload["events"]],
            roles=sorted(
                {role["name"] for item in payload["instructions"] for role in item["accounts"]}
            ),
        )
        for program_id, payload in catalog().items()
    }


def instruction(
    *,
    program_id: str = PUMP_ID,
    name: str = "create",
    index: int = 4,
    inner_index: int | None = None,
    args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    definition = next(
        item for item in catalog()[program_id]["instructions"] if item["name"] == name
    )
    if args is None:
        args = {
            "name": "Example Token",
            "symbol": "EXAMPLE",
            "uri": "https://example.com/token.json",
            "creator": ADDRESS,
        }
    return {
        "instructionIndex": index,
        "innerInstructionIndex": inner_index,
        "stackHeight": 1 if inner_index is None else 2,
        "programId": program_id,
        "programName": "pump" if program_id == PUMP_ID else "pump_amm",
        "instructionName": name,
        "summary": None,
        "decoded": {
            "args": args,
            "accounts": [
                {"name": role["name"], "pubkey": ADDRESS, "isSigner": False, "isWritable": True}
                for role in definition["accounts"]
            ],
        },
    }


def payload() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "parsedTransactionNotification",
        "params": {
            "subscription": 23,
            "result": {
                "context": {"slot": 123},
                "value": {
                    "transaction": {
                        "signature": SIGNATURE,
                        "slot": 123,
                        "blockTime": None,
                        "feePayer": ADDRESS,
                        "fee": 5000,
                        "accountKeys": [ADDRESS],
                        "status": "ok",
                        "error": None,
                        "summary": None,
                        "nativeTransfers": [],
                        "tokenTransfers": [],
                    },
                    "instructions": [instruction()],
                    "matchedIndexes": [0],
                },
            },
        },
    }


def value(data: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = data["params"]["result"]["value"]
    return result


def decode(data: dict[str, Any], *, discovered: dict[str, ProgramDescription] | None = None) -> Any:
    return ParsedStreamsDecoder(SETTINGS, discovered or descriptions()).decode(
        DecoderInput(raw_event_id=42, transport="parsed_streams", payload_json=json.dumps(data))
    )


def envelope(data: dict[str, Any]) -> Any:
    return notification_envelope(
        data, subscription_id=23, received_at=RECEIVED_AT, idl_revision=REVISION
    )


def test_full_notification_decodes_pinned_create_without_fabricating_events() -> None:
    result = decode(payload())
    assert result.status == DecodeStatus.DECODED
    assert result.raw_event_id == 42
    assert result.events == []
    assert len(result.instructions) == 1
    decoded = result.instructions[0]
    assert decoded.program_name == ProgramName.PUMP
    assert decoded.instruction_name == "create"
    assert decoded.instruction_index == 4
    assert decoded.is_inner is False
    assert decoded.matched_filter is True
    assert decoded.args["creator"] == ADDRESS
    assert decoded.accounts[0].name == "mint"


def test_inner_instruction_index_and_filter_match_use_different_positions() -> None:
    data = payload()
    value(data)["instructions"] = [instruction(index=7), instruction(index=7, inner_index=0)]
    value(data)["matchedIndexes"] = [1]
    result = decode(data)
    assert result.status == DecodeStatus.DECODED
    assert [item.instruction_index for item in result.instructions] == [7, 7]
    assert [item.is_inner for item in result.instructions] == [False, True]
    assert [item.matched_filter for item in result.instructions] == [False, True]
    assert (
        envelope(data).raw_payload["params"]["result"]["value"]["instructions"][1][
            "innerInstructionIndex"
        ]
        == 0
    )


@pytest.mark.parametrize("program_id", [PUMP_ID, PUMP_AMM_ID])
@pytest.mark.parametrize("raw_amount", [2**64 - 1, str(2**64 - 1)])
def test_unsigned_amounts_remain_exact_with_original_integer_or_string_type(
    program_id: str, raw_amount: int | str
) -> None:
    args = (
        {"amount": raw_amount, "min_sol_output": "0"}
        if program_id == PUMP_ID
        else {"base_amount_in": raw_amount, "min_quote_amount_out": "0"}
    )
    data = payload()
    value(data)["instructions"] = [instruction(program_id=program_id, name="sell", args=args)]
    result = decode(data)
    assert result.status == DecodeStatus.DECODED
    amount_name = "amount" if program_id == PUMP_ID else "base_amount_in"
    assert result.instructions[0].args[amount_name] == raw_amount
    assert type(result.instructions[0].args[amount_name]) is type(raw_amount)


@pytest.mark.parametrize("bad_amount", [1.0, True, -1, 2**64, "01", "1.0", "-1", "1e10"])
def test_raw_unsigned_amounts_reject_float_coercion_boolean_and_out_of_range(
    bad_amount: Any,
) -> None:
    data = payload()
    value(data)["instructions"] = [
        instruction(name="sell", args={"amount": bad_amount, "min_sol_output": "0"})
    ]
    assert decode(data).status == DecodeStatus.FAILED


def test_undocumented_complex_idl_types_are_quarantined_without_guessing_serialization() -> None:
    data = payload()
    value(data)["instructions"] = [
        instruction(
            name="buy",
            args={
                "amount": "1",
                "max_sol_cost": "2",
                "track_volume": [True],
                "partial_fill": [False],
            },
        )
    ]
    result = decode(data)
    assert result.status == DecodeStatus.RAW_ONLY
    assert result.safe_error_code == "unsupported_argument_type"
    assert result.instructions == []
    assert envelope(data).raw_payload == data


@pytest.mark.parametrize(
    "path",
    [
        (),
        ("params",),
        ("params", "result"),
        ("params", "result", "context"),
        ("params", "result", "value"),
        ("params", "result", "value", "transaction"),
        ("params", "result", "value", "instructions", 0),
        ("params", "result", "value", "instructions", 0, "decoded"),
        ("params", "result", "value", "instructions", 0, "decoded", "args"),
        ("params", "result", "value", "instructions", 0, "decoded", "accounts", 0),
    ],
)
def test_unknown_wire_or_argument_fields_remain_raw_without_partial_decoding(
    path: tuple[Any, ...],
) -> None:
    data = payload()
    location: Any = data
    for part in path:
        location = location[part]
    location["futureField"] = {"secret-shaped": "do-not-log"}
    result = decode(data)
    assert result.status == DecodeStatus.RAW_ONLY
    assert result.safe_error_code == "unknown_fields"
    assert result.instructions == []
    assert result.events == []
    assert "do-not-log" not in str(result)
    assert envelope(data).raw_payload == data


def test_unknown_summary_structure_is_quarantined_but_protocol_parsed_data_is_opaque() -> None:
    data = payload()
    value(data)["transaction"]["summary"] = {
        "type": "swap",
        "description": "Venue-reported context",
        "parsedData": {"future_venue_key": {"amount": "999999999999999999999"}},
    }
    assert decode(data).status == DecodeStatus.DECODED
    value(data)["transaction"]["summary"]["newSummaryField"] = True
    assert decode(data).safe_error_code == "unknown_fields"


def test_unknown_account_role_quarantines_whole_transaction() -> None:
    data = payload()
    value(data)["instructions"][0]["decoded"]["accounts"][0]["name"] = "new_provider_role"
    assert decode(data).safe_error_code == "unknown_fields"


@pytest.mark.parametrize(
    "change",
    [
        "missing_arg",
        "missing_account",
        "duplicate_account",
        "wrong_order",
        "wrong_bool",
        "bad_pubkey",
    ],
)
def test_malformed_known_instruction_fields_fail(change: str) -> None:
    data = payload()
    decoded = value(data)["instructions"][0]["decoded"]
    if change == "missing_arg":
        del decoded["args"]["creator"]
    elif change == "missing_account":
        decoded["accounts"].pop()
    elif change == "duplicate_account":
        decoded["accounts"].append(copy.deepcopy(decoded["accounts"][0]))
    elif change == "wrong_order":
        decoded["accounts"].reverse()
    elif change == "wrong_bool":
        decoded["accounts"][0]["isSigner"] = 1
    else:
        decoded["accounts"][0]["pubkey"] = "invalid-sensitive-value"
    result = decode(data)
    assert result.status == DecodeStatus.FAILED
    assert result.safe_error_code == "malformed_notification"
    assert "invalid-sensitive-value" not in str(result)


@pytest.mark.parametrize("new_name", ["new_provider_instruction", "CreateEvent"])
def test_provider_new_names_cannot_expand_pinned_instruction_support(new_name: str) -> None:
    discovered = descriptions()
    discovered[PUMP_ID].instructions.append(new_name)
    data = payload()
    value(data)["instructions"][0]["instructionName"] = new_name
    result = decode(data, discovered=discovered)
    assert result.status == DecodeStatus.RAW_ONLY
    assert result.safe_error_code == "unknown_instruction"


def test_known_pin_name_requires_successful_provider_discovery() -> None:
    discovered = descriptions()
    discovered[PUMP_ID].instructions.remove("create")
    assert decode(payload(), discovered=discovered).safe_error_code == "unknown_instruction"


def test_known_account_role_requires_successful_provider_discovery() -> None:
    discovered = descriptions()
    discovered[PUMP_ID].roles.remove("mint")
    result = decode(payload(), discovered=discovered)
    assert result.status == DecodeStatus.RAW_ONLY
    assert result.safe_error_code == "catalog_name_mismatch"


def test_program_name_is_checked_against_provider_description_without_guessing() -> None:
    data = payload()
    value(data)["instructions"][0]["programName"] = "wrong_provider_program"
    assert decode(data).safe_error_code == "catalog_name_mismatch"
    discovered = descriptions()
    discovered[PUMP_ID].name = "provider_pump_name"
    value(data)["instructions"][0]["programName"] = "provider_pump_name"
    assert decode(data, discovered=discovered).status == DecodeStatus.DECODED


def test_unknown_monitored_instruction_quarantines_preceding_supported_output() -> None:
    data = payload()
    unknown = instruction(index=5)
    unknown["instructionName"] = "new_instruction"
    value(data)["instructions"].append(unknown)
    assert decode(data).instructions == []
    assert decode(data).status == DecodeStatus.RAW_ONLY


def test_unrelated_raw_instruction_keeps_context_without_affecting_supported_instruction() -> None:
    data = payload()
    value(data)["instructions"].append(
        {
            "instructionIndex": 5,
            "innerInstructionIndex": None,
            "stackHeight": 1,
            "programId": ADDRESS,
            "programName": None,
            "instructionName": None,
            "summary": None,
            "decoded": None,
            "rawData": "123",
            "rawAccounts": [ADDRESS],
        }
    )
    result = decode(data)
    assert result.status == DecodeStatus.DECODED
    assert len(result.instructions) == 1
    assert envelope(data).raw_payload == data


def test_unrelated_decoded_instruction_args_remain_opaque_context() -> None:
    data = payload()
    unrelated = instruction(index=5)
    unrelated["programId"] = ADDRESS
    unrelated["programName"] = "unrelated"
    unrelated["instructionName"] = "unverified"
    unrelated["decoded"]["args"] = {"unverified_key": {"wire_value": 1.5}}
    value(data)["instructions"].append(unrelated)
    assert decode(data).status == DecodeStatus.DECODED


def test_failed_transaction_preserves_structured_error_without_typed_effects() -> None:
    data = payload()
    value(data)["transaction"].update(
        status="error", error={"InstructionError": [4, {"Custom": 6001}]}
    )
    result = decode(data)
    assert result.status == DecodeStatus.RAW_ONLY
    assert result.safe_error_code == "failed_transaction"
    assert result.instructions == []
    assert envelope(data).transaction_error == {"InstructionError": [4, {"Custom": 6001}]}


@pytest.mark.parametrize("indices", [[1], [-1], [True], [0, 0], ["0"]])
def test_bad_matched_array_positions_fail(indices: list[Any]) -> None:
    data = payload()
    value(data)["matchedIndexes"] = indices
    assert decode(data).status == DecodeStatus.FAILED


@pytest.mark.parametrize(
    "change",
    [
        "duplicate_top",
        "descending_top",
        "orphan_inner",
        "wrong_parent",
        "duplicate_inner",
        "descending_inner",
        "bad_stack",
    ],
)
def test_invalid_instruction_execution_order_fails(change: str) -> None:
    data = payload()
    instructions = value(data)["instructions"]
    if change == "duplicate_top":
        instructions.append(instruction())
    elif change == "descending_top":
        instructions.append(instruction(index=3))
    elif change == "orphan_inner":
        instructions[0] = instruction(inner_index=0)
    elif change == "wrong_parent":
        instructions.append(instruction(index=5, inner_index=0))
    elif change == "duplicate_inner":
        instructions.extend([instruction(inner_index=0), instruction(inner_index=0)])
    elif change == "descending_inner":
        instructions.extend([instruction(inner_index=1), instruction(inner_index=0)])
    else:
        instructions[0]["stackHeight"] = 2
    assert decode(data).status == DecodeStatus.FAILED


@pytest.mark.parametrize(
    "change",
    [
        "slot_mismatch",
        "signature",
        "status_error",
        "status_ok",
        "fee_payer",
        "missing_field",
        "bool_slot",
        "bool_fee",
    ],
)
def test_invalid_transaction_context_fails(change: str) -> None:
    data = payload()
    transaction = value(data)["transaction"]
    if change == "slot_mismatch":
        transaction["slot"] = 999
    elif change == "signature":
        transaction["signature"] = "invalid-provider-secret"
    elif change == "status_error":
        transaction["status"] = "error"
    elif change == "status_ok":
        transaction["error"] = {"InstructionError": [0, "failure"]}
    elif change == "fee_payer":
        transaction["feePayer"] = PUMP_ID
    elif change == "missing_field":
        del transaction["nativeTransfers"]
    elif change == "bool_slot":
        transaction["slot"] = True
    else:
        transaction["fee"] = True
    result = decode(data)
    assert result.status == DecodeStatus.FAILED
    assert "invalid-provider-secret" not in str(result)


def test_documented_transfers_preserve_raw_integer_amounts_and_reject_float_amounts() -> None:
    data = payload()
    value(data)["transaction"]["nativeTransfers"] = [
        {"fromUserAccount": ADDRESS, "toUserAccount": PUMP_ID, "amount": 1000000}
    ]
    value(data)["transaction"]["tokenTransfers"] = [
        {
            "fromUserAccount": ADDRESS,
            "toUserAccount": PUMP_ID,
            "fromTokenAccount": ADDRESS,
            "toTokenAccount": PUMP_ID,
            "rawTokenAmount": "18446744073709551615",
            "decimals": 6,
            "tokenStandard": "Fungible",
            "mint": PUMP_AMM_ID,
        }
    ]
    assert decode(data).status == DecodeStatus.DECODED
    value(data)["transaction"]["tokenTransfers"][0]["rawTokenAmount"] = 1.5
    assert decode(data).status == DecodeStatus.FAILED


def test_unknown_transfer_fields_are_raw_only() -> None:
    data = payload()
    value(data)["transaction"]["nativeTransfers"] = [
        {
            "fromUserAccount": ADDRESS,
            "toUserAccount": PUMP_ID,
            "amount": 1000000,
            "future": True,
        }
    ]
    assert decode(data).safe_error_code == "unknown_fields"


@pytest.mark.parametrize(
    "serialized",
    [
        "null",
        "[]",
        "broken-json-secret",
        '{"jsonrpc":"2.0","jsonrpc":"2.0"}',
        '{"unknown":NaN}',
        '{"unknown":1e309}',
    ],
)
def test_invalid_or_ambiguous_json_fails_with_safe_diagnostic(serialized: str) -> None:
    result = ParsedStreamsDecoder(SETTINGS, descriptions()).decode(
        DecoderInput(raw_event_id=42, transport="parsed_streams", payload_json=serialized)
    )
    assert result.status == DecodeStatus.FAILED
    assert result.safe_error_code == "invalid_payload"
    assert "broken-json-secret" not in str(result)


def test_unsupported_transport_is_explicit_raw_only() -> None:
    result = ParsedStreamsDecoder(SETTINGS, descriptions()).decode(
        DecoderInput(raw_event_id=42, transport="fixture", payload_json=json.dumps(payload()))
    )
    assert result.status == DecodeStatus.RAW_ONLY
    assert result.safe_error_code == "unsupported_transport"


def test_description_catalog_is_required_for_each_configured_program() -> None:
    discovered = descriptions()
    del discovered[PUMP_AMM_ID]
    with pytest.raises(DecoderConfigurationError, match="discovery is incomplete"):
        ParsedStreamsDecoder(SETTINGS, discovered)


@pytest.mark.parametrize(
    "field,bad",
    [
        ("id", "bad-secret-address"),
        ("name", "bad name"),
        ("instructions", ["bad-name"]),
        ("roles", [True]),
        ("events", ["TradeEvent", "TradeEvent"]),
        ("future", True),
    ],
)
def test_program_description_rejects_bad_addresses_names_duplicates_and_unknown_fields(
    field: str, bad: Any
) -> None:
    data = descriptions()[PUMP_ID].model_dump()
    data[field] = bad
    with pytest.raises(ValidationError):
        ProgramDescription.model_validate(data)


def test_envelope_hash_is_stable_covers_unknown_body_and_retains_distinct_revisions() -> None:
    data = payload()
    original = envelope(data)
    assert original.commitment == Commitment.CONFIRMED
    expected_hash = hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert original.raw_payload_sha256 == expected_hash
    assert original.notification_id == f"parsed-streams:{expected_hash}"
    assert envelope(copy.deepcopy(data)).notification_id == original.notification_id
    data["providerExtension"] = {"preserved": True}
    changed = envelope(data)
    assert changed.notification_id != original.notification_id
    assert original.raw_payload != data
    assert changed.raw_payload == data


@pytest.mark.parametrize(
    "change",
    [
        "rpc",
        "method",
        "subscription",
        "bool_subscription",
        "slot",
        "bool_slot",
        "signature",
        "nan",
        "bad_datetime",
        "revision",
    ],
)
def test_envelope_minimum_identity_rejects_invalid_data_with_fixed_safe_error(change: str) -> None:
    data = payload()
    received_at = RECEIVED_AT
    revision = REVISION
    if change == "rpc":
        data["jsonrpc"] = "secret"
    elif change == "method":
        data["method"] = "secret"
    elif change == "subscription":
        data["params"]["subscription"] = 999
    elif change == "bool_subscription":
        data["params"]["subscription"] = True
    elif change == "slot":
        value(data)["transaction"]["slot"] = 999
    elif change == "bool_slot":
        data["params"]["result"]["context"]["slot"] = True
    elif change == "signature":
        value(data)["transaction"]["signature"] = "secret"
    elif change == "nan":
        data["future"] = float("nan")
    elif change == "bad_datetime":
        received_at = datetime(2026, 10, 13)
    else:
        revision = ""
    with pytest.raises(ParsedStreamModelError) as error:
        notification_envelope(
            data, subscription_id=23, received_at=received_at, idl_revision=revision
        )
    assert str(error.value) == "parsed stream notification identity is invalid"
    assert error.value.__cause__ is None


def test_identity_valid_malformed_body_is_retained_before_decode() -> None:
    data = payload()
    del value(data)["instructions"][0]["decoded"]["args"]["creator"]
    assert envelope(data).raw_payload == data
    assert decode(data).status == DecodeStatus.FAILED


def test_pipeline_journals_unknown_notification_before_decode_without_losing_original_body() -> (
    None
):
    data = payload()
    value(data)["instructions"][0]["decoded"]["args"]["future_argument"] = "stored-before-decode"
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:

        class ObservingDecoder:
            def decode(self, event: DecoderInput) -> Any:
                stored = session.execute(
                    select(raw_events).where(raw_events.c.id == event.raw_event_id)
                ).first()
                assert stored is not None
                assert json.loads(stored.payload_json) == data
                return ParsedStreamsDecoder(SETTINGS, descriptions()).decode(event)

        pipeline = DecodePipeline(
            RawJournal(session), ObservingDecoder(), DecodeOutcomeRepository(session)
        )
        result = pipeline.process(envelope(data))
        assert result.status == DecodeStatus.RAW_ONLY
        assert result.safe_error_code == "unknown_fields"
        stored = session.execute(select(raw_events)).one()
        assert json.loads(stored.payload_json) == data
        assert stored.status == "raw_only"
