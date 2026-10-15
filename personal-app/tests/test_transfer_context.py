"""Compiled account resolution and runtime provenance regression tests."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from app.transfer_context import TransferContextError, parse_transfer_context

PAYER = "6EF8rrecthR5DkFwoB6aMR4NjDqCwgpEVARfY5hCYJH"
DESTINATION = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
SYSTEM = "11111111111111111111111111111111"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ATA = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
COMPUTE = "ComputeBudget111111111111111111111111111111"
PRECOMPILES = [
    "Ed25519SigVerify111111111111111111111111111",
    "KeccakSecp256k11111111111111111111111111111",
    "Secp256r1SigVerify1111111111111111111111111",
]


def instruction(
    program: int, accounts: list[int] | None = None, height: int | None = None
) -> dict[str, Any]:
    return {
        "programIdIndex": program,
        "accounts": accounts or [],
        "data": "1",
        "stackHeight": height,
    }


def payload() -> dict[str, Any]:
    return {
        "slot": 12,
        "transactionIndex": 3,
        "blockTime": 1700000000,
        "version": "legacy",
        "transaction": {
            "signatures": ["1" * 64],
            "message": {
                "accountKeys": [PAYER, DESTINATION, SYSTEM, TOKEN, ATA],
                "header": {
                    "numRequiredSignatures": 1,
                    "numReadonlySignedAccounts": 0,
                    "numReadonlyUnsignedAccounts": 3,
                },
                "instructions": [instruction(2, [0, 1], 1)],
                "recentBlockhash": SYSTEM,
            },
        },
        "meta": {
            "err": None,
            "innerInstructions": [],
            "logMessages": None,
            "loadedAddresses": {"writable": [], "readonly": []},
        },
    }


def with_inner() -> dict[str, Any]:
    data = payload()
    data["transaction"]["message"]["instructions"] = [instruction(4, [0, 1], 1)]
    data["meta"]["innerInstructions"] = [
        {"index": 0, "instructions": [instruction(2, [0, 1], 2), instruction(3, [0, 1], 2)]}
    ]
    data["meta"]["logMessages"] = [
        f"Program {ATA} invoke [1]",
        f"Program {SYSTEM} invoke [2]",
        f"Program {SYSTEM} success",
        f"Program {TOKEN} invoke [2]",
        f"Program {TOKEN} success",
        f"Program {ATA} success",
    ]
    return data


def test_legacy_top_level_context_is_exact_and_immutable() -> None:
    data = payload()
    original = deepcopy(data)
    context = parse_transfer_context(data)
    assert context.account_keys == (PAYER, DESTINATION, SYSTEM, TOKEN, ATA)
    assert context.fee_payer == PAYER
    assert context.signers == (PAYER,)
    assert context.warnings == ()
    assert not context.transaction_failed
    fact = context.instructions[0]
    assert fact.program_id == SYSTEM
    assert fact.accounts == (PAYER, DESTINATION)
    assert fact.account_indices == (0, 1)
    assert fact.top_level_index == 0
    assert fact.inner_instruction_index is None
    assert fact.execution_status == "succeeded"
    assert data == original
    with pytest.raises(FrozenInstanceError):
        context.fee_payer = DESTINATION  # type: ignore[misc]


def test_runtime_cpi_missing_from_compiled_groups_is_unverified() -> None:
    data = with_inner()
    data["meta"]["innerInstructions"] = []
    result = parse_transfer_context(data)
    assert result.warnings == ("inner_execution_unverified",)
    assert len(result.instructions) == 1
    assert result.instructions[0].execution_status == "succeeded"


def test_v0_loaded_addresses_follow_static_writable_then_readonly() -> None:
    data = payload()
    data["version"] = 0
    message = data["transaction"]["message"]
    message["accountKeys"] = [PAYER]
    message["header"]["numReadonlyUnsignedAccounts"] = 0
    message["addressTableLookups"] = [
        {"accountKey": ATA, "writableIndexes": [3], "readonlyIndexes": [6, 7]}
    ]
    message["instructions"] = [instruction(3, [0, 1])]
    data["meta"]["loadedAddresses"] = {"writable": [DESTINATION], "readonly": [TOKEN, SYSTEM]}
    result = parse_transfer_context(data)
    assert result.account_keys == (PAYER, DESTINATION, TOKEN, SYSTEM)
    assert result.instructions[0].program_id == SYSTEM
    assert result.instructions[0].accounts == (PAYER, DESTINATION)
    assert result.signers == (PAYER,)


def test_v0_lookup_address_metadata_unavailable_is_explicit() -> None:
    data = payload()
    data["version"] = 0
    data["transaction"]["message"]["addressTableLookups"] = [
        {"accountKey": ATA, "writableIndexes": [0], "readonlyIndexes": []}
    ]
    del data["meta"]["loadedAddresses"]
    with pytest.raises(TransferContextError) as raised:
        parse_transfer_context(data)
    assert raised.value.code == "account_resolution_unavailable"


def config() -> dict[str, Any]:
    return {
        "computeUnitLimit": 30000,
        "heapSize": None,
        "loadedAccountsDataSizeLimit": 200000,
        "priorityFee": (1 << 64) - 1,
    }


def test_v1_config_inline_keys_preserve_large_integer_fee() -> None:
    data = payload()
    data["version"] = 1
    data["transaction"]["message"]["transactionConfig"] = config()
    assert parse_transfer_context(data).instructions[0].program_id == SYSTEM


@pytest.mark.parametrize("missing", [False, True])
def test_unavailable_version_warns_without_inference(missing: bool) -> None:
    data = payload()
    if missing:
        del data["version"]
    else:
        data["version"] = None
    assert parse_transfer_context(data).warnings == ("transaction_version_unavailable",)


@pytest.mark.parametrize("shape", ["lookup", "config"])
def test_unavailable_version_resolves_documented_shape_with_warning(shape: str) -> None:
    data = payload()
    data["version"] = None
    message = data["transaction"]["message"]
    if shape == "config":
        message["transactionConfig"] = config()
    else:
        message["addressTableLookups"] = [
            {"accountKey": ATA, "writableIndexes": [0], "readonlyIndexes": []}
        ]
        data["meta"]["loadedAddresses"]["writable"] = [COMPUTE]
    assert parse_transfer_context(data).warnings == ("transaction_version_unavailable",)


@pytest.mark.parametrize("version", [True, False, 2, -1, "0", "1", 1.0, [], {}])
def test_unknown_transaction_versions_rejected(version: Any) -> None:
    data = payload()
    data["version"] = version
    with pytest.raises(TransferContextError):
        parse_transfer_context(data)


@pytest.mark.parametrize("version", ["legacy", 0])
@pytest.mark.parametrize("value", [None, config()])
def test_legacy_v0_forbid_any_transaction_config(version: Any, value: Any) -> None:
    data = payload()
    data["version"] = version
    data["transaction"]["message"]["transactionConfig"] = value
    with pytest.raises(TransferContextError):
        parse_transfer_context(data)


@pytest.mark.parametrize(
    "change",
    [
        "absent",
        "null",
        "missing_field",
        "unknown_field",
        "bool",
        "negative",
        "overflow",
        "string",
        "lookups",
    ],
)
def test_invalid_v1_config_or_alt_context_is_rejected(change: str) -> None:
    data = payload()
    data["version"] = 1
    message = data["transaction"]["message"]
    message["transactionConfig"] = config()
    if change == "absent":
        del message["transactionConfig"]
    elif change == "null":
        message["transactionConfig"] = None
    elif change == "missing_field":
        del message["transactionConfig"]["heapSize"]
    elif change == "unknown_field":
        message["transactionConfig"]["secret"] = "private"
    elif change == "lookups":
        message["addressTableLookups"] = []
    else:
        message["transactionConfig"]["computeUnitLimit"] = {
            "bool": True,
            "negative": -1,
            "overflow": 1 << 32,
            "string": "1",
        }[change]
    with pytest.raises(TransferContextError):
        parse_transfer_context(data)


@pytest.mark.parametrize("version", ["legacy", 1])
def test_non_v0_loaded_addresses_rejected(version: Any) -> None:
    data = payload()
    data["version"] = version
    if version == 1:
        data["transaction"]["message"]["transactionConfig"] = config()
    data["meta"]["loadedAddresses"]["writable"] = [COMPUTE]
    with pytest.raises(TransferContextError):
        parse_transfer_context(data)


@pytest.mark.parametrize(
    "change",
    ["count_mismatch", "unbacked_addresses", "bool_index", "large_index", "unknown_lookup"],
)
def test_inconsistent_lookup_resolution_rejected(change: str) -> None:
    data = payload()
    data["version"] = 0
    message = data["transaction"]["message"]
    message["addressTableLookups"] = [
        {"accountKey": ATA, "writableIndexes": [0], "readonlyIndexes": []}
    ]
    data["meta"]["loadedAddresses"]["writable"] = [COMPUTE]
    if change == "count_mismatch":
        data["meta"]["loadedAddresses"]["writable"] = []
    elif change == "unbacked_addresses":
        del message["addressTableLookups"]
    elif change == "unknown_lookup":
        message["addressTableLookups"][0]["secret"] = "private"
    else:
        message["addressTableLookups"][0]["writableIndexes"] = [
            True if change == "bool_index" else 256
        ]
    with pytest.raises(TransferContextError):
        parse_transfer_context(data)


def test_missing_header_keeps_fee_payer_and_marks_signers_unknown() -> None:
    data = payload()
    del data["transaction"]["message"]["header"]
    result = parse_transfer_context(data)
    assert result.fee_payer == PAYER
    assert result.signers is None
    assert result.warnings == ("signer_context_unavailable",)


def test_duplicate_resolved_accounts_rejected_without_collapsing_balance_indices() -> None:
    data = payload()
    data["transaction"]["message"]["accountKeys"][1] = PAYER
    with pytest.raises(TransferContextError):
        parse_transfer_context(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("numRequiredSignatures", 2),
        ("numRequiredSignatures", 0),
        ("numRequiredSignatures", True),
        ("numReadonlySignedAccounts", 1),
        ("numReadonlyUnsignedAccounts", 5),
        ("numReadonlyUnsignedAccounts", -1),
    ],
)
def test_invalid_header_counts_rejected(field: str, value: Any) -> None:
    data = payload()
    data["transaction"]["message"]["header"][field] = value
    with pytest.raises(TransferContextError):
        parse_transfer_context(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("programIdIndex", True),
        ("programIdIndex", -1),
        ("programIdIndex", 5),
        ("accounts", [False]),
        ("accounts", ["0"]),
        ("data", "0"),
        ("data", ["base64", "AQ=="]),
        ("stackHeight", 2),
        ("parsed", {}),
    ],
)
def test_compiled_instruction_context_is_strict(field: str, value: Any) -> None:
    data = payload()
    data["transaction"]["message"]["instructions"][0][field] = value
    with pytest.raises(TransferContextError):
        parse_transfer_context(data)


def test_verified_inner_order_and_ata_ancestor_context() -> None:
    result = parse_transfer_context(with_inner())
    assert [item.program_id for item in result.instructions] == [ATA, SYSTEM, TOKEN]
    assert [item.inner_instruction_index for item in result.instructions] == [None, 0, 1]
    assert all(item.execution_status == "succeeded" for item in result.instructions)
    assert result.instructions[1].ancestor_programs == (ATA,)
    assert result.instructions[2].ancestor_programs == (ATA,)
    assert not result.warnings


def test_nested_runtime_context_preserves_all_ancestor_programs() -> None:
    data = with_inner()
    data["meta"]["innerInstructions"][0]["instructions"][1]["stackHeight"] = 3
    data["meta"]["logMessages"] = [
        f"Program {ATA} invoke [1]",
        f"Program {SYSTEM} invoke [2]",
        f"Program {TOKEN} invoke [3]",
        f"Program {TOKEN} success",
        f"Program {SYSTEM} success",
        f"Program {ATA} success",
    ]
    result = parse_transfer_context(data)
    assert result.instructions[2].ancestor_programs == (ATA, SYSTEM)
    assert result.instructions[2].execution_status == "succeeded"


def test_caught_failed_cpi_does_not_affect_successful_sibling() -> None:
    data = with_inner()
    data["meta"]["logMessages"][2] = f"Program {SYSTEM} failed: custom program error: 0x1"
    result = parse_transfer_context(data)
    assert [item.execution_status for item in result.instructions] == [
        "succeeded",
        "failed",
        "succeeded",
    ]


def test_parent_cpi_failure_marks_earlier_successful_descendants_failed() -> None:
    data = with_inner()
    data["meta"]["innerInstructions"][0]["instructions"][1]["stackHeight"] = 3
    data["meta"]["logMessages"] = [
        f"Program {ATA} invoke [1]",
        f"Program {SYSTEM} invoke [2]",
        f"Program {TOKEN} invoke [3]",
        f"Program {TOKEN} success",
        f"Program {SYSTEM} failed: custom program error: 0x1",
        f"Program {ATA} success",
    ]
    result = parse_transfer_context(data)
    assert [item.execution_status for item in result.instructions] == [
        "succeeded",
        "failed",
        "failed",
    ]


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "empty",
        "truncated",
        "unbalanced",
        "mismatched_exit",
        "wrong_order",
        "wrong_depth",
        "huge_depth",
        "missing_inner",
        "extra_inner",
        "malformed_control",
        "failed_top",
        "not_string",
    ],
)
def test_incomplete_or_uncorrelated_logs_make_all_inner_execution_unverified(change: str) -> None:
    data = with_inner()
    logs = data["meta"]["logMessages"]
    if change == "missing":
        del data["meta"]["logMessages"]
    elif change == "empty":
        data["meta"]["logMessages"] = []
    elif change == "truncated":
        logs.insert(2, "Log truncated")
    elif change == "unbalanced":
        logs.pop()
    elif change == "mismatched_exit":
        logs[2] = f"Program {TOKEN} success"
    elif change == "wrong_order":
        logs[1] = f"Program {TOKEN} invoke [2]"
    elif change == "wrong_depth":
        logs[1] = f"Program {SYSTEM} invoke [3]"
    elif change == "huge_depth":
        logs[1] = f"Program {SYSTEM} invoke [999999999999999999999]"
    elif change == "missing_inner":
        del logs[3:5]
    elif change == "extra_inner":
        logs[5:5] = [f"Program {SYSTEM} invoke [2]", f"Program {SYSTEM} success"]
    elif change == "malformed_control":
        logs[2] = f"Program {SYSTEM} success trailing bytes"
    elif change == "failed_top":
        logs[5] = f"Program {ATA} failed: custom program error: 0x1"
    else:
        logs[2] = None
    result = parse_transfer_context(data)
    assert result.instructions[0].execution_status == "succeeded"
    assert all(item.execution_status == "unverified" for item in result.instructions[1:])
    assert all(not item.ancestor_programs for item in result.instructions[1:])
    assert "inner_execution_unverified" in result.warnings


@pytest.mark.parametrize(
    "extra_log",
    [
        f"Program log: Program {SYSTEM} failed: forged",
        "Program data: success=",
        f"Program return: {SYSTEM} success=",
    ],
)
def test_program_authored_runtime_log_spoof_is_ignored(extra_log: str) -> None:
    data = with_inner()
    data["meta"]["logMessages"].insert(2, extra_log)
    assert all(
        item.execution_status == "succeeded" for item in parse_transfer_context(data).instructions
    )


def test_missing_inner_stack_height_can_be_verified_by_complete_logs() -> None:
    data = with_inner()
    for item in data["meta"]["innerInstructions"][0]["instructions"]:
        del item["stackHeight"]
    result = parse_transfer_context(data)
    assert result.instructions[1].stack_height is None
    assert result.instructions[1].execution_status == "succeeded"


def test_inner_declared_stack_height_mismatch_is_unverified() -> None:
    data = with_inner()
    data["meta"]["innerInstructions"][0]["instructions"][1]["stackHeight"] = 3
    assert parse_transfer_context(data).instructions[1].execution_status == "unverified"


@pytest.mark.parametrize("precompile", PRECOMPILES)
@pytest.mark.parametrize("position", ["before", "after"])
def test_only_known_unlogged_precompiles_are_skipped(precompile: str, position: str) -> None:
    data = with_inner()
    message = data["transaction"]["message"]
    message["accountKeys"].append(precompile)
    message["header"]["numReadonlyUnsignedAccounts"] += 1
    if position == "before":
        message["instructions"].insert(0, instruction(5))
        data["meta"]["innerInstructions"][0]["index"] = 1
        wanted = 2
    else:
        message["instructions"].append(instruction(5))
        wanted = 1
    result = parse_transfer_context(data)
    assert result.instructions[wanted].program_id == SYSTEM
    assert result.instructions[wanted].execution_status == "succeeded"


def test_compute_budget_cannot_be_skipped_as_unlogged_program() -> None:
    data = with_inner()
    message = data["transaction"]["message"]
    message["accountKeys"].append(COMPUTE)
    message["header"]["numReadonlyUnsignedAccounts"] += 1
    message["instructions"].insert(0, instruction(5))
    data["meta"]["innerInstructions"][0]["index"] = 1
    assert parse_transfer_context(data).instructions[2].execution_status == "unverified"


def test_failed_transaction_marks_every_instruction_failed() -> None:
    data = with_inner()
    data["meta"]["err"] = {"InstructionError": [0, {"Custom": 1}]}
    result = parse_transfer_context(data)
    assert result.transaction_failed
    assert all(item.execution_status == "failed" for item in result.instructions)


@pytest.mark.parametrize("inner", [None, "absent"])
def test_unavailable_inner_metadata_has_explicit_warning(inner: Any) -> None:
    data = payload()
    if inner == "absent":
        del data["meta"]["innerInstructions"]
    else:
        data["meta"]["innerInstructions"] = None
    assert parse_transfer_context(data).warnings == ("inner_instruction_context_unavailable",)


@pytest.mark.parametrize("change", ["duplicate", "unordered", "outside", "bool", "unknown"])
def test_invalid_inner_groups_are_rejected(change: str) -> None:
    data = with_inner()
    groups = data["meta"]["innerInstructions"]
    if change in ("duplicate", "unordered"):
        data["transaction"]["message"]["instructions"].append(instruction(2))
        groups.append(deepcopy(groups[0]))
        if change == "unordered":
            groups[0]["index"] = 1
    elif change == "outside":
        groups[0]["index"] = 1
    elif change == "bool":
        groups[0]["index"] = False
    else:
        groups[0]["hidden"] = "secret-provider-response"
    with pytest.raises(TransferContextError):
        parse_transfer_context(data)


@pytest.mark.parametrize(
    "shape",
    [
        "keys",
        "instructions",
        "combined_instructions",
        "logs",
        "instruction_bytes",
        "log_bytes",
        "depth",
    ],
)
def test_resource_limits_are_enforced(shape: str) -> None:
    data = with_inner()
    message = data["transaction"]["message"]
    if shape == "keys":
        message["accountKeys"] = [PAYER] * 257
    elif shape == "instructions":
        message["instructions"] = [instruction(2)] * 1025
    elif shape == "combined_instructions":
        message["instructions"] = [instruction(2)] * 1024
    elif shape == "logs":
        data["meta"]["logMessages"] = ["log"] * 8193
    elif shape == "instruction_bytes":
        message["instructions"][0]["data"] = "1" * 4097
    elif shape == "log_bytes":
        data["meta"]["logMessages"] = ["x" * 131073]
    else:
        data["meta"]["innerInstructions"][0]["instructions"][0]["stackHeight"] = 17
    with pytest.raises(TransferContextError):
        parse_transfer_context(data)


def test_untrusted_content_never_appears_in_error_message() -> None:
    data = payload()
    data["transaction"]["message"]["accountKeys"][0] = "secret-provider-content"
    with pytest.raises(TransferContextError) as raised:
        parse_transfer_context(data)
    assert "secret-provider-content" not in str(raised.value)
    assert raised.value.code == "invalid_transfer_context"
