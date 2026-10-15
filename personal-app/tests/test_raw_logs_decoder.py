"""Binary fixture decoding and adversarial runtime execution provenance."""

from __future__ import annotations

import base64
import copy
import json
import struct
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.config import BASE58_ALPHABET, Settings
from app.decoders import DecoderInput
from app.domain.enums import Commitment, DecodeStatus
from app.integrations.raw_logs_decoder import RawLogsDecoder
from app.normalization import PumpCreateEventNormalizer, PumpTradeEventNormalizer
from app.pumpswap import PumpSwapEventNormalizer

ROOT = Path(__file__).resolve().parents[2]
SETTINGS = Settings()
PUMP = SETTINGS.pump_program_id
AMM = SETTINGS.pump_amm_program_id
OTHER = "So11111111111111111111111111111111111111112"
SIGNATURE = "1" * 64
EVENTS = [
    (PUMP, "CreateEvent"),
    (PUMP, "TradeEvent"),
    (PUMP, "CompleteEvent"),
    (PUMP, "PostCompleteBuyEvent"),
    (PUMP, "CompletePumpAmmMigrationEvent"),
    (AMM, "CreatePoolEvent"),
    (AMM, "BuyEvent"),
    (AMM, "SellEvent"),
]
IDLS = {
    PUMP: json.loads((ROOT / "personal-app/catalog/idl/pump.json").read_text()),
    AMM: json.loads((ROOT / "personal-app/catalog/idl/pump_amm.json").read_text()),
}


def pubkey_bytes(address: str) -> bytes:
    number = 0
    for character in address:
        number = number * 58 + BASE58_ALPHABET.index(character)
    return number.to_bytes(32, "big")


def base58_bytes(data: bytes) -> str:
    number = int.from_bytes(data, "big")
    characters: list[str] = []
    while number:
        number, remainder = divmod(number, 58)
        characters.append(BASE58_ALPHABET[remainder])
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + "".join(reversed(characters))


def definition(program: str, name: str) -> dict[str, Any]:
    return next(item["type"] for item in IDLS[program]["types"] if item["name"] == name)


def fields(program: str, event_name: str, **overrides: Any) -> dict[str, Any]:
    """Fill newer required fixture fields explicitly, only in test data."""
    values: dict[str, Any] = {}
    for field in definition(program, event_name)["fields"]:
        kind = field["type"]
        if kind == "pubkey":
            value: Any = OTHER
        elif kind == "string":
            value = "buy" if field["name"] == "ix_name" else "fixture"
        elif kind == "bool":
            value = False
        elif isinstance(kind, dict) and "vec" in kind:
            value = []
        elif kind in ("u64", "i128", "u128"):
            value = "0"
        else:
            value = 1791853200 if kind == "i64" else 0
        values[field["name"]] = value
    values.update(overrides)
    return values


def encode_type(program: str, kind: Any, value: Any) -> bytes:
    """Test-only serializer, never used by the application."""
    if kind == "pubkey":
        return pubkey_bytes(value)
    if kind == "bool":
        assert type(value) is bool
        return struct.pack("B", value)
    if kind == "string":
        encoded = value.encode()
        assert len(encoded) <= 16384
        return struct.pack("<I", len(encoded)) + encoded
    if isinstance(kind, str):
        return int(value).to_bytes(int(kind[1:]) // 8, "little", signed=kind.startswith("i"))
    if "vec" in kind:
        assert len(value) <= 1024
        return struct.pack("<I", len(value)) + b"".join(
            encode_type(program, kind["vec"], item) for item in value
        )
    if "defined" in kind:
        return encode_fields(program, kind["defined"]["name"], value)
    raise AssertionError("test serializer received an unsupported pinned field")


def encode_fields(program: str, name: str, values: dict[str, Any]) -> bytes:
    return b"".join(
        encode_type(program, field["type"], values[field["name"]])
        for field in definition(program, name)["fields"]
    )


def event_bytes(program: str, name: str, values: dict[str, Any] | None = None) -> bytes:
    event = next(item for item in IDLS[program]["events"] if item["name"] == name)
    return bytes(event["discriminator"]) + encode_fields(
        program, name, fields(program, name) if values is None else values
    )


def data_log(data: bytes) -> str:
    return "Program data: " + base64.b64encode(data).decode()


def compiled(program_index: int, stack_height: int | None = None) -> dict[str, Any]:
    return {
        "programIdIndex": program_index,
        "accounts": [],
        "data": "",
        "stackHeight": stack_height,
    }


def payload(program: str = PUMP, name: str = "CreateEvent") -> dict[str, Any]:
    return {
        "slot": 300000001,
        "blockTime": 1791853200,
        "version": "legacy",
        "transaction": {
            "signatures": [SIGNATURE],
            "message": {"accountKeys": [OTHER, program], "instructions": [compiled(1)]},
        },
        "meta": {
            "err": None,
            "innerInstructions": [],
            "loadedAddresses": {"writable": [], "readonly": []},
            "logMessages": [
                f"Program {program} invoke [1]",
                data_log(event_bytes(program, name)),
                f"Program {program} success",
            ],
            "fee": 5000,
        },
    }


def decode(data: Any):
    return RawLogsDecoder(SETTINGS).decode(
        DecoderInput(raw_event_id=42, transport="logs_subscribe", payload_json=json.dumps(data))
    )


@pytest.mark.parametrize("program,name", EVENTS)
def test_every_monitored_pinned_event_decodes_exact_fields(program: str, name: str) -> None:
    original = payload(program, name)
    saved = copy.deepcopy(original)
    result = decode(original)
    assert result.status == DecodeStatus.DECODED
    assert result.raw_event_id == 42
    assert result.instructions == []
    assert len(result.events) == 1
    event = result.events[0]
    assert event.event_name == name
    assert event.program_id == program
    assert event.fields == fields(program, name)
    assert event.event_index == event.source_instruction_index == 0
    assert original == saved


@pytest.mark.parametrize(
    "filename,golden_filename,normalizer",
    [
        ("pump-create-event.json", "create-event.expected.json", PumpCreateEventNormalizer),
        ("pump-trade-event.json", "trade-event.expected.json", PumpTradeEventNormalizer),
        ("pumpswap-create-pool-event.json", "pool-event.expected.json", PumpSwapEventNormalizer),
    ],
)
def test_binary_golden_fixtures_reach_existing_normalizers(
    filename: str, golden_filename: str, normalizer: Any
) -> None:
    fixture = json.loads((ROOT / "coding-pack/fixtures/helius" / filename).read_text())
    raw = fixture["raw_payload"]
    program = PUMP if raw["program"] == "pump" else AMM
    name = raw["eventName"]
    values = fields(program, name, **raw["decoded"])
    data = payload(program, name)
    data["meta"]["logMessages"][1] = data_log(event_bytes(program, name, values))
    decoded = decode(data).events[0]
    assert decoded.fields == values
    canonical = normalizer(SETTINGS).normalize(
        decoded,
        provider="helius",
        signature=fixture["signature"],
        slot=fixture["slot"],
        commitment=Commitment(fixture["commitment"]),
        observed_at=datetime(2026, 10, 13, tzinfo=UTC),
        raw_event_id=42,
    )
    output = canonical.model_dump(mode="json")
    output["base_mint"] = canonical.mint
    golden = json.loads((ROOT / "coding-pack/golden-tests" / golden_filename).read_text())
    assert {key: output[key] for key in golden} == golden


def test_independently_packed_completion_event_and_public_key_leading_zeros() -> None:
    data = payload(PUMP, "CompleteEvent")
    # Exact pinned discriminator + three pubkeys + little-endian i64 + quote pubkey.
    raw = bytes([95, 114, 97, 156, 212, 46, 152, 8]) + b"\0" * 96
    raw += struct.pack("<q", -(1 << 63)) + pubkey_bytes(OTHER)
    data["meta"]["logMessages"][1] = data_log(raw)
    event = decode(data).events[0]
    assert event.fields["timestamp"] == -(1 << 63)
    assert event.fields["mint"] == "1" * 32
    assert event.fields["quote_mint"] == OTHER


@pytest.mark.parametrize("name", ["BuyEvent", "SellEvent"])
@pytest.mark.parametrize("reserve", [-(1 << 127), -10, 0, 10, (1 << 127) - 1])
def test_signed_reserves_and_max_u64_are_exact(name: str, reserve: int) -> None:
    values = fields(AMM, name, virtual_quote_reserves=str(reserve), base_supply=str((1 << 64) - 1))
    data = payload(AMM, name)
    data["meta"]["logMessages"][1] = data_log(event_bytes(AMM, name, values))
    event = decode(data).events[0]
    assert event.fields["virtual_quote_reserves"] == str(reserve)
    assert event.fields["base_supply"] == str((1 << 64) - 1)
    assert type(event.fields["timestamp"]) is int
    assert type(event.fields["can_boost"]) is bool


def test_defined_shareholders_vector_decodes_in_order_without_numeric_coercion() -> None:
    values = fields(
        PUMP,
        "TradeEvent",
        shareholders=[
            {"address": "1" * 32, "share_bps": 65535},
            {"address": OTHER, "share_bps": 1},
        ],
    )
    data = payload(PUMP, "TradeEvent")
    data["meta"]["logMessages"][1] = data_log(event_bytes(PUMP, "TradeEvent", values))
    event = decode(data).events[0]
    assert event.fields["shareholders"] == values["shareholders"]


def test_event_indices_span_programs_repeated_types_and_cpi_execution_order() -> None:
    data = payload()
    message = data["transaction"]["message"]
    message["accountKeys"] = [OTHER, PUMP, AMM]
    message["instructions"] = [compiled(0), compiled(1)]
    data["meta"]["innerInstructions"] = [
        {"index": 0, "instructions": [compiled(1, 2), compiled(2, 3)]}
    ]
    data["meta"]["logMessages"] = [
        f"Program {OTHER} invoke [1]",
        f"Program {PUMP} invoke [2]",
        data_log(event_bytes(PUMP, "TradeEvent")),
        f"Program {AMM} invoke [3]",
        data_log(event_bytes(AMM, "BuyEvent")),
        f"Program {AMM} success",
        data_log(event_bytes(PUMP, "TradeEvent")),
        f"Program {PUMP} success",
        f"Program {OTHER} success",
        f"Program {PUMP} invoke [1]",
        data_log(event_bytes(PUMP, "CompleteEvent")),
        f"Program {PUMP} success",
    ]
    result = decode(data)
    assert result.status == DecodeStatus.DECODED
    assert [event.event_index for event in result.events] == [0, 1, 2, 3]
    assert [event.source_instruction_index for event in result.events] == [0, 0, 0, 1]
    assert [event.program_id for event in result.events] == [PUMP, AMM, PUMP, PUMP]


def test_v0_loaded_addresses_follow_static_writable_readonly_order() -> None:
    data = payload()
    data["version"] = 0
    message = data["transaction"]["message"]
    message["accountKeys"] = [OTHER]
    message["instructions"][0]["programIdIndex"] = 2
    message["addressTableLookups"] = [
        {"accountKey": OTHER, "writableIndexes": [0], "readonlyIndexes": [1]}
    ]
    data["meta"]["loadedAddresses"] = {"writable": [AMM], "readonly": [PUMP]}
    assert decode(data).events[0].program_id == PUMP


def v1_payload() -> dict[str, Any]:
    data = payload()
    data["version"] = 1
    data["transaction"]["message"]["transactionConfig"] = {
        "computeUnitLimit": 30000,
        "heapSize": None,
        "loadedAccountsDataSizeLimit": 200000,
        "priorityFee": 2**64 - 1,
    }
    return data


def test_v1_inline_addresses_and_config_decode_without_changing_exact_integers() -> None:
    data = v1_payload()
    saved = copy.deepcopy(data)
    result = decode(data)
    assert result.status == DecodeStatus.DECODED
    assert result.events[0].program_id == PUMP
    assert data == saved


@pytest.mark.parametrize("change", ["missing", "null", "missing_field", "unknown_field"])
def test_v1_requires_the_known_complete_transaction_config(change: str) -> None:
    data = v1_payload()
    message = data["transaction"]["message"]
    if change == "missing":
        del message["transactionConfig"]
    elif change == "null":
        message["transactionConfig"] = None
    elif change == "missing_field":
        del message["transactionConfig"]["heapSize"]
    else:
        message["transactionConfig"]["futureLimit"] = 1
    result = decode(data)
    assert not result.events
    assert result.status == (
        DecodeStatus.RAW_ONLY if change == "unknown_field" else DecodeStatus.FAILED
    )


@pytest.mark.parametrize(
    "field,width",
    [
        ("computeUnitLimit", 32),
        ("heapSize", 32),
        ("loadedAccountsDataSizeLimit", 32),
        ("priorityFee", 64),
    ],
)
@pytest.mark.parametrize("value", [None, 0, "maximum", -1, "overflow", True, "1", 1.0])
def test_v1_transaction_config_checks_unsigned_widths_and_types(
    field: str, width: int, value: Any
) -> None:
    data = v1_payload()
    if value == "maximum":
        value = 2**width - 1
    elif value == "overflow":
        value = 2**width
    data["transaction"]["message"]["transactionConfig"][field] = value
    result = decode(data)
    valid = value is None or (type(value) is int and 0 <= value < 2**width)
    assert result.status == (DecodeStatus.DECODED if valid else DecodeStatus.FAILED)
    assert bool(result.events) == valid


@pytest.mark.parametrize("version", ["legacy", 0])
@pytest.mark.parametrize("value", [None, {}])
def test_earlier_versions_reject_v1_config_even_when_null(version: Any, value: Any) -> None:
    data = payload()
    data["version"] = version
    data["transaction"]["message"]["transactionConfig"] = value
    assert decode(data).status == DecodeStatus.FAILED


@pytest.mark.parametrize("change", ["empty_lookups", "lookups", "loaded_addresses"])
def test_v1_cannot_resolve_programs_through_address_lookup_tables(change: str) -> None:
    data = v1_payload()
    message = data["transaction"]["message"]
    if change == "empty_lookups":
        message["addressTableLookups"] = []
    elif change == "lookups":
        message["addressTableLookups"] = [
            {"accountKey": OTHER, "writableIndexes": [], "readonlyIndexes": [0]}
        ]
    if change != "empty_lookups":
        message["accountKeys"] = [OTHER]
        data["meta"]["loadedAddresses"]["readonly"] = [PUMP]
    result = decode(data)
    assert result.status == DecodeStatus.FAILED
    assert not result.events


@pytest.mark.parametrize(
    "precompile",
    [
        "Ed25519SigVerify111111111111111111111111111",
        "KeccakSecp256k11111111111111111111111111111",
        "Secp256r1SigVerify1111111111111111111111111",
    ],
)
def test_unlogged_verified_precompiles_preserve_source_instruction_position(
    precompile: str,
) -> None:
    data = payload()
    message = data["transaction"]["message"]
    message["accountKeys"].append(precompile)
    message["instructions"] = [compiled(2), compiled(1), compiled(2)]
    assert decode(data).events[0].source_instruction_index == 1


def test_missing_arbitrary_top_level_instruction_cannot_shift_source_index() -> None:
    data = payload()
    data["transaction"]["message"]["instructions"].insert(0, compiled(0))
    result = decode(data)
    assert result.status == DecodeStatus.RAW_ONLY
    assert result.safe_error_code == "incomplete_logs"
    assert not result.events


def test_pump_event_bytes_from_an_unrelated_program_are_not_pump_events() -> None:
    data = payload()
    data["transaction"]["message"]["instructions"] = [compiled(0)]
    data["meta"]["logMessages"][0] = f"Program {OTHER} invoke [1]"
    data["meta"]["logMessages"][-1] = f"Program {OTHER} success"
    assert decode(data).safe_error_code == "no_supported_event"


@pytest.mark.parametrize(
    "change", ["depth", "orphan_exit", "wrong_exit", "orphan_data", "bad_depth"]
)
def test_spoofed_or_unbalanced_execution_is_rejected(change: str) -> None:
    data = payload()
    logs = data["meta"]["logMessages"]
    if change == "depth":
        logs[0] = f"Program {PUMP} invoke [2]"
    elif change == "orphan_exit":
        logs.insert(0, f"Program {PUMP} success")
    elif change == "wrong_exit":
        logs[-1] = f"Program {AMM} success"
    elif change == "orphan_data":
        logs.append(logs[1])
    else:
        logs[0] = f"Program {PUMP} invoke [01]"
    result = decode(data)
    assert result.status == DecodeStatus.FAILED
    assert result.safe_error_code == "malformed_log_stack"
    assert not result.events


@pytest.mark.parametrize("change", ["missing_exit", "truncation", "missing_final_instruction"])
def test_incomplete_log_evidence_discards_preceding_decoded_events(change: str) -> None:
    data = payload()
    if change == "missing_exit":
        data["meta"]["logMessages"].pop()
    elif change == "truncation":
        data["meta"]["logMessages"].append("Log truncated")
    else:
        data["transaction"]["message"]["instructions"].append(compiled(1))
    result = decode(data)
    assert result.status == DecodeStatus.RAW_ONLY
    assert not result.events


@pytest.mark.parametrize("change", ["wrong_program", "wrong_depth", "missing", "extra"])
def test_cpi_context_order_and_stack_height_are_verified(change: str) -> None:
    data = payload()
    data["meta"]["innerInstructions"] = [{"index": 0, "instructions": [compiled(1, 2)]}]
    logs = data["meta"]["logMessages"]
    logs.insert(1, f"Program {PUMP} invoke [2]")
    logs.insert(-1, f"Program {PUMP} success")
    if change == "wrong_program":
        data["meta"]["innerInstructions"][0]["instructions"][0]["programIdIndex"] = 0
    elif change == "wrong_depth":
        data["meta"]["innerInstructions"][0]["instructions"][0]["stackHeight"] = 3
    elif change == "missing":
        data["meta"]["innerInstructions"] = None
    else:
        data["meta"]["innerInstructions"][0]["instructions"].append(compiled(1, 2))
    result = decode(data)
    assert result.status in (DecodeStatus.FAILED, DecodeStatus.RAW_ONLY)
    assert not result.events


@pytest.mark.parametrize("failed_meta", [False, True])
def test_failed_execution_has_no_typed_effects_even_after_binary_event(failed_meta: bool) -> None:
    data = payload()
    if failed_meta:
        data["meta"]["err"] = {"InstructionError": [0, {"Custom": 6001}]}
    else:
        data["meta"]["logMessages"][-1] = f"Program {PUMP} failed: secret-provider-detail"
    result = decode(data)
    assert result.status == DecodeStatus.RAW_ONLY
    assert not result.events
    assert "secret-provider-detail" not in str(result)


@pytest.mark.parametrize(
    "line",
    [
        "Program log: Instruction: CreateV2",
        "Program log: Program data: malicious",
        "Program log: CreateEvent",
    ],
)
def test_instruction_text_is_never_fabricated_into_an_emitted_event(line: str) -> None:
    data = payload()
    data["meta"]["logMessages"][1] = line
    result = decode(data)
    assert result.status == DecodeStatus.RAW_ONLY
    assert result.safe_error_code == "no_supported_event"


@pytest.mark.parametrize("binary", [b"\xff" * 8, bytes(IDLS[PUMP]["events"][0]["discriminator"])])
def test_unknown_or_unmonitored_event_data_quarantines_whole_transaction(binary: bytes) -> None:
    data = payload()
    data["meta"]["logMessages"].insert(2, data_log(binary))
    result = decode(data)
    assert result.status == DecodeStatus.RAW_ONLY
    assert result.safe_error_code == "unknown_event"
    assert not result.events


@pytest.mark.parametrize("bad", ["not-base64-secret", "AAAA", "", "AA== AA=="])
def test_malformed_binary_has_fixed_safe_error_and_no_output(bad: str) -> None:
    data = payload()
    data["meta"]["logMessages"][1] = "Program data: " + bad
    result = decode(data)
    assert result.status == DecodeStatus.FAILED
    assert result.safe_error_code == "malformed_event_data"
    assert "secret" not in str(result)


@pytest.mark.parametrize(
    "change", ["truncated", "extra", "invalid_bool", "invalid_utf8", "huge_string"]
)
def test_borsh_boundary_and_layout_errors_fail_without_partial_output(change: str) -> None:
    binary = event_bytes(PUMP, "TradeEvent")
    if change == "truncated":
        binary = binary[:-1]
    elif change == "extra":
        binary += b"\x00"
    elif change == "invalid_bool":
        # discriminator + mint + sol amount + token amount => is_buy.
        binary = binary[:56] + b"\x02" + binary[57:]
    else:
        binary = event_bytes(PUMP, "CreateEvent")
        binary = binary[:8] + (
            struct.pack("<I", 16385) if change == "huge_string" else struct.pack("<I", 1) + b"\xff"
        )
    data = payload()
    data["meta"]["logMessages"][1] = data_log(binary)
    result = decode(data)
    assert result.status in (DecodeStatus.FAILED, DecodeStatus.RAW_ONLY)
    assert not result.events


def test_huge_vector_length_cannot_allocate_unbounded_memory() -> None:
    values = fields(PUMP, "TradeEvent")
    prefix = bytes(
        next(
            event["discriminator"]
            for event in IDLS[PUMP]["events"]
            if event["name"] == "TradeEvent"
        )
    )
    for field in definition(PUMP, "TradeEvent")["fields"]:
        if field["name"] == "shareholders":
            break
        prefix += encode_type(PUMP, field["type"], values[field["name"]])
    data = payload()
    data["meta"]["logMessages"][1] = data_log(prefix + struct.pack("<I", (1 << 32) - 1))
    result = decode(data)
    assert result.safe_error_code == "event_resource_limit"
    assert not result.events


@pytest.mark.parametrize(
    "change",
    [
        "bool_slot",
        "negative_slot",
        "bad_signature",
        "bool_program_index",
        "bad_account_index",
        "missing_logs",
        "parsed_keys",
        "wrong_lookup_count",
        "wrong_signature_count",
        "bool_header_count",
        "header_unsigned_overflow",
        "invalid_blockhash",
        "bool_block_time",
    ],
)
def test_malformed_full_transaction_identity_and_compiled_context_fail(change: str) -> None:
    data = payload()
    message = data["transaction"]["message"]
    if change == "bool_slot":
        data["slot"] = True
    elif change == "negative_slot":
        data["slot"] = -1
    elif change == "bad_signature":
        data["transaction"]["signatures"] = ["secret"]
    elif change == "bool_program_index":
        message["instructions"][0]["programIdIndex"] = True
    elif change == "bad_account_index":
        message["instructions"][0]["accounts"] = [200]
    elif change == "missing_logs":
        del data["meta"]["logMessages"]
    elif change == "parsed_keys":
        message["accountKeys"][0] = {"pubkey": OTHER, "signer": True, "writable": True}
    elif change == "wrong_lookup_count":
        data["version"] = 0
        message["addressTableLookups"] = [
            {"accountKey": OTHER, "writableIndexes": [0], "readonlyIndexes": []}
        ]
    elif change in ("wrong_signature_count", "bool_header_count", "header_unsigned_overflow"):
        message["header"] = {
            "numRequiredSignatures": 1,
            "numReadonlySignedAccounts": 0,
            "numReadonlyUnsignedAccounts": 1,
        }
        if change == "wrong_signature_count":
            message["header"]["numRequiredSignatures"] = 2
        elif change == "bool_header_count":
            message["header"]["numReadonlySignedAccounts"] = True
        else:
            message["header"]["numReadonlyUnsignedAccounts"] = 2
    elif change == "invalid_blockhash":
        message["recentBlockhash"] = "secret"
    else:
        data["blockTime"] = True
    result = decode(data)
    assert result.status == DecodeStatus.FAILED
    assert not result.events
    assert "secret" not in str(result)


@pytest.mark.parametrize("location", ["root", "transaction", "message", "instruction", "loaded"])
def test_unknown_execution_context_fields_are_quarantined(location: str) -> None:
    data = payload()
    target = {
        "root": data,
        "transaction": data["transaction"],
        "message": data["transaction"]["message"],
        "instruction": data["transaction"]["message"]["instructions"][0],
        "loaded": data["meta"]["loadedAddresses"],
    }[location]
    target["futureContext"] = "secret"
    result = decode(data)
    assert result.status == DecodeStatus.RAW_ONLY
    assert result.safe_error_code == "unknown_context_fields"
    assert not result.events
    assert "secret" not in str(result)


def test_unrelated_meta_fields_remain_opaque_raw_context() -> None:
    data = payload()
    data["meta"]["futureMeasurement"] = {"provider": 1.25}
    assert decode(data).status == DecodeStatus.DECODED


@pytest.mark.parametrize("version", [True, 2, "0", None])
def test_unsupported_versions_are_explicit_raw_only(version: Any) -> None:
    data = payload()
    data["version"] = version
    assert decode(data).safe_error_code == "unsupported_transaction_version"


@pytest.mark.parametrize(
    "serialized",
    ["null", "[]", "broken-secret", '{"slot":0,"slot":1}', '{"slot":NaN}', '{"slot":1e999}'],
)
def test_ambiguous_or_nonfinite_json_is_rejected(serialized: str) -> None:
    result = RawLogsDecoder(SETTINGS).decode(
        DecoderInput(raw_event_id=42, transport="logs_subscribe", payload_json=serialized)
    )
    assert result.status == DecodeStatus.FAILED
    assert result.safe_error_code == "invalid_payload"
    assert "secret" not in str(result)


def test_decoder_does_not_accept_a_different_transport() -> None:
    result = RawLogsDecoder(SETTINGS).decode(
        DecoderInput(raw_event_id=42, transport="fixture", payload_json=json.dumps(payload()))
    )
    assert result.status == DecodeStatus.RAW_ONLY
    assert result.safe_error_code == "unsupported_transport"


def cpi_payload(
    program: str = PUMP, name: str = "TradeEvent", *, router: bool = False
) -> dict[str, Any]:
    data = payload(program, name)
    instruction = compiled(1, 3 if router else 2)
    instruction["data"] = base58_bytes(
        bytes.fromhex("e445a52e51cb9a1d") + event_bytes(program, name)
    )
    instruction["accounts"] = [0]
    data["meta"]["innerInstructions"] = [{"index": 0, "instructions": [instruction]}]
    logs = [
        f"Program {program} invoke [1]",
        f"Program {program} invoke [2]",
        f"Program {program} success",
        f"Program {program} success",
    ]
    if router:
        data["transaction"]["message"]["instructions"] = [compiled(0)]
        data["meta"]["innerInstructions"][0]["instructions"].insert(0, compiled(1, 2))
        logs[0] = f"Program {program} invoke [2]"
        logs[1] = f"Program {program} invoke [3]"
        logs.insert(0, f"Program {OTHER} invoke [1]")
        logs.append(f"Program {OTHER} success")
    data["meta"]["logMessages"] = logs
    return data


@pytest.mark.parametrize("program,name", EVENTS)
@pytest.mark.parametrize("router", [False, True])
def test_anchor_event_cpi_is_decoded_from_correlated_successful_self_invocation(
    program: str, name: str, router: bool
) -> None:
    original = cpi_payload(program, name, router=router)
    saved = copy.deepcopy(original)
    result = decode(original)
    assert result.status == DecodeStatus.DECODED
    assert len(result.events) == 1
    event = result.events[0]
    assert event.event_name == name
    assert event.program_id == program
    assert event.fields == fields(program, name)
    assert event.event_index == event.source_instruction_index == 0
    assert original == saved


def test_binary_log_and_separate_event_cpi_share_transaction_execution_indices() -> None:
    data = cpi_payload()
    logs = data["meta"]["logMessages"]
    logs.insert(1, data_log(event_bytes(PUMP, "CreateEvent")))
    logs.insert(-1, data_log(event_bytes(PUMP, "TradeEvent")))
    result = decode(data)
    assert result.status == DecodeStatus.DECODED
    assert [event.event_name for event in result.events] == [
        "CreateEvent",
        "TradeEvent",
        "TradeEvent",
    ]
    assert [event.event_index for event in result.events] == [0, 1, 2]
    assert [event.source_instruction_index for event in result.events] == [0, 0, 0]


@pytest.mark.parametrize(
    "change", ["different_parent", "different_target", "top_level", "missing_authority"]
)
def test_event_cpi_requires_a_monitored_self_invocation(change: str) -> None:
    data = cpi_payload()
    if change == "different_parent":
        data["transaction"]["message"]["instructions"] = [compiled(0)]
        data["meta"]["logMessages"][0] = f"Program {OTHER} invoke [1]"
        data["meta"]["logMessages"][-1] = f"Program {OTHER} success"
    elif change == "different_target":
        data["transaction"]["message"]["accountKeys"].append(AMM)
        data["meta"]["innerInstructions"][0]["instructions"][0]["programIdIndex"] = 2
        data["meta"]["logMessages"][1] = f"Program {AMM} invoke [2]"
        data["meta"]["logMessages"][2] = f"Program {AMM} success"
    elif change == "top_level":
        inner = data["meta"]["innerInstructions"][0]["instructions"][0]
        inner["stackHeight"] = 1
        data["transaction"]["message"]["instructions"] = [inner]
        data["meta"]["innerInstructions"] = []
        data["meta"]["logMessages"] = [f"Program {PUMP} invoke [1]", f"Program {PUMP} success"]
    else:
        data["meta"]["innerInstructions"][0]["instructions"][0]["accounts"] = []
    result = decode(data)
    assert result.status == DecodeStatus.RAW_ONLY
    assert result.safe_error_code == "ambiguous_event_provenance"
    assert not result.events


@pytest.mark.parametrize("change", ["wrong_tag", "unknown_event", "truncated", "trailing_bytes"])
def test_event_cpi_requires_exact_anchor_tag_and_complete_pinned_event_layout(change: str) -> None:
    data = cpi_payload()
    tag = bytes.fromhex("e445a52e51cb9a1d")
    raw = tag + event_bytes(PUMP, "TradeEvent")
    if change == "wrong_tag":
        raw = b"\xff" + raw[1:]
    elif change == "unknown_event":
        raw = tag + b"\xff" * 8
    elif change == "truncated":
        raw = raw[:-1]
    else:
        raw += b"\0"
    data["meta"]["innerInstructions"][0]["instructions"][0]["data"] = base58_bytes(raw)
    result = decode(data)
    assert result.status in (DecodeStatus.FAILED, DecodeStatus.RAW_ONLY)
    assert not result.events
    assert (
        result.safe_error_code
        == {
            "wrong_tag": "no_supported_event",
            "unknown_event": "unknown_event",
            "truncated": "malformed_event_data",
            "trailing_bytes": "unsupported_event_layout",
        }[change]
    )


@pytest.mark.parametrize("change", ["data_log", "nested_invocation"])
def test_event_cpi_frame_cannot_have_conflicting_event_representations(change: str) -> None:
    data = cpi_payload()
    if change == "data_log":
        data["meta"]["logMessages"].insert(2, data_log(event_bytes(PUMP, "TradeEvent")))
    else:
        data["meta"]["innerInstructions"][0]["instructions"].append(compiled(1, 3))
        data["meta"]["logMessages"][2:2] = [f"Program {PUMP} invoke [3]", f"Program {PUMP} success"]
    result = decode(data)
    assert result.status == DecodeStatus.RAW_ONLY
    assert result.safe_error_code == "ambiguous_event_representation"
    assert not result.events


@pytest.mark.parametrize("scope", ["event", "parent", "transaction"])
def test_event_cpi_is_discarded_if_its_scope_or_ancestor_fails(scope: str) -> None:
    data = cpi_payload()
    if scope == "transaction":
        data["meta"]["err"] = {"InstructionError": [0, "Custom"]}
    else:
        data["meta"]["logMessages"][2 if scope == "event" else 3] = (
            f"Program {PUMP} failed: provider-secret"
        )
    result = decode(data)
    assert result.status == DecodeStatus.RAW_ONLY
    assert not result.events
    assert "provider-secret" not in str(result)
