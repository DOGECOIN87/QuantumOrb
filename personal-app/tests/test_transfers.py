"""Independent binary fixtures for wallet transfer facts and evidence boundaries."""

from __future__ import annotations

import copy
import json
import struct
from dataclasses import FrozenInstanceError

import pytest

from app.config import BASE58_ALPHABET
from app.domain.enums import EvidenceState
from app.integrations.wallet_history_models import parse_history_transaction
from app.transfers import TransferExtractor


def base58(data):
    number = int.from_bytes(data, "big")
    encoded = ""
    while number:
        number, digit = divmod(number, 58)
        encoded = BASE58_ALPHABET[digit] + encoded
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + encoded


def address(number):
    return base58(number.to_bytes(32, "big"))


PAYER, DESTINATION, MINT = (address(index) for index in (1, 2, 3))
SOURCE_TOKEN, DESTINATION_TOKEN = (address(index) for index in (4, 5))
SOURCE_OWNER, DESTINATION_OWNER, DELEGATE = (address(index) for index in (6, 7, 8))
PARENT, HOOK = (address(index) for index in (9, 10))
SYSTEM = "11111111111111111111111111111111"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SIGNATURE = base58((1).to_bytes(64, "big"))
KEYS = [
    PAYER,
    DESTINATION,
    MINT,
    SOURCE_TOKEN,
    DESTINATION_TOKEN,
    SOURCE_OWNER,
    DESTINATION_OWNER,
    DELEGATE,
    PARENT,
    HOOK,
    SYSTEM,
    TOKEN,
    TOKEN_2022,
    ATA,
]
U64_MAX = (1 << 64) - 1


def transaction(*, keys=None, error=None):
    keys = list(KEYS if keys is None else keys)
    return {
        "slot": 300000007,
        "transactionIndex": 4,
        "blockTime": 1791935940,
        "version": "legacy",
        "transaction": {
            "signatures": [SIGNATURE],
            "message": {
                "header": {
                    "numRequiredSignatures": 1,
                    "numReadonlySignedAccounts": 0,
                    "numReadonlyUnsignedAccounts": 0,
                },
                "accountKeys": keys,
                "recentBlockhash": address(11),
                "instructions": [],
            },
        },
        "meta": {
            "err": error,
            "fee": 5000,
            "preBalances": [1_000_000] * len(keys),
            "postBalances": [995_000] + [1_000_000] * (len(keys) - 1),
            "preTokenBalances": [],
            "postTokenBalances": [],
            "innerInstructions": [],
            "logMessages": [],
        },
    }


def instruction(program, accounts, data, *, keys=KEYS, stack_height=None):
    result = {
        "programIdIndex": keys.index(program),
        "accounts": [keys.index(account) for account in accounts],
        "data": base58(data),
    }
    if stack_height is not None:
        result["stackHeight"] = stack_height
    return result


def system_transfer(amount, source=PAYER, destination=DESTINATION, **options):
    return instruction(SYSTEM, [source, destination], struct.pack("<IQ", 2, amount), **options)


def token_transfer(amount, *, checked=False, program=TOKEN, decimals=6, **options):
    accounts = [SOURCE_TOKEN, DESTINATION_TOKEN, DELEGATE]
    data = b"\x03" + struct.pack("<Q", amount)
    if checked:
        accounts = [SOURCE_TOKEN, MINT, DESTINATION_TOKEN, DELEGATE]
        data = b"\x0c" + struct.pack("<QB", amount, decimals)
    return instruction(program, accounts, data, **options)


def token_balance(data, account, amount, *, owner=None, mint=MINT, decimals=6, program=TOKEN):
    keys = data["transaction"]["message"]["accountKeys"]
    loaded = data["meta"].get("loadedAddresses", {})
    keys = keys + loaded.get("writable", []) + loaded.get("readonly", [])
    result = {
        "accountIndex": keys.index(account),
        "mint": mint,
        "programId": program,
        "uiTokenAmount": {
            "amount": str(amount),
            "decimals": decimals,
            "uiAmount": None,
            "uiAmountString": "not-used-for-exact-amounts",
        },
    }
    if owner is not None:
        result["owner"] = owner
    return result


def token_metadata(data, amount=7, *, program=TOKEN, fee=0, source_owner=SOURCE_OWNER):
    data["meta"]["preTokenBalances"] = [
        token_balance(data, SOURCE_TOKEN, 100, owner=source_owner, program=program),
        token_balance(data, DESTINATION_TOKEN, 10, owner=DESTINATION_OWNER, program=program),
    ]
    data["meta"]["postTokenBalances"] = [
        token_balance(data, SOURCE_TOKEN, 100 - amount, owner=source_owner, program=program),
        token_balance(
            data, DESTINATION_TOKEN, 10 + amount - fee, owner=DESTINATION_OWNER, program=program
        ),
    ]
    return data


def inner_transaction(inner, *, parent=PARENT, logs=None, parent_data=b"\x01"):
    data = transaction()
    data["transaction"]["message"]["instructions"] = [instruction(parent, [], parent_data)]
    data["meta"]["innerInstructions"] = [{"index": 0, "instructions": list(inner)}]
    data["meta"]["logMessages"] = (
        logs
        if logs is not None
        else [
            f"Program {parent} invoke [1]",
            f"Program {SYSTEM} invoke [2]",
            f"Program {SYSTEM} success",
            f"Program {parent} success",
        ]
    )
    return data


def history(data, *, raw_event_id=1, page_raw_event_id=2):
    return parse_history_transaction(
        data, raw_event_id=raw_event_id, page_raw_event_id=page_raw_event_id
    )


def extract(data, **options):
    return TransferExtractor().extract(history(data, **options))


def test_native_transfer_preserves_direction_exact_amount_and_provenance():
    data = transaction()
    amount = U64_MAX - 5000
    data["transaction"]["message"]["instructions"] = [system_transfer(amount)]
    data["meta"]["preBalances"][0] = U64_MAX
    data["meta"]["postBalances"][0] = 0
    data["meta"]["preBalances"][1] = 0
    data["meta"]["postBalances"][1] = amount
    result = extract(data)
    assert result.state is EvidenceState.OBSERVED
    assert result.complete
    assert len(result.transfers) == 1
    fact = result.transfers[0]
    assert fact.asset == "sol" and fact.program_id == SYSTEM
    assert fact.source_account == PAYER and fact.destination_account == DESTINATION
    assert fact.amount_raw == str(amount)
    assert fact.source_owner is None and fact.destination_owner is None
    assert fact.mint is None
    assert fact.top_level_instruction_index == 0 and fact.inner_instruction_index is None
    assert fact.context == "transfer"
    assert fact.signature == result.signature == SIGNATURE
    assert fact.slot == result.slot == data["slot"]
    assert fact.transaction_index == result.transaction_index == 4
    assert fact.block_time == result.block_time == data["blockTime"]
    assert fact.raw_event_id == result.raw_event_id == 1
    assert fact.page_raw_event_id == result.page_raw_event_id == 2
    assert result.fee_payer == PAYER and result.signers == (PAYER,)
    assert result.fee_lamports_raw == "5000" and result.account_keys == tuple(KEYS)


def test_native_balance_delta_keeps_fee_and_only_payer_gets_fee_adjustment():
    data = transaction()
    data["transaction"]["message"]["instructions"] = [system_transfer(700)]
    data["meta"]["postBalances"][0] -= 700
    data["meta"]["postBalances"][1] += 700
    result = extract(data)
    changes = {change.account: change for change in result.native_balance_changes}
    assert set(changes) == {PAYER, DESTINATION}
    assert changes[PAYER].delta_raw == "-5700"
    assert changes[PAYER].fee_adjusted_delta_raw == "-700"
    assert changes[DESTINATION].delta_raw == "700"
    assert changes[DESTINATION].fee_adjusted_delta_raw == "700"
    assert changes[PAYER].pre_amount_raw == "1000000"
    assert changes[PAYER].post_amount_raw == "994300"
    assert changes[PAYER].state is EvidenceState.OBSERVED


def test_system_transfer_with_seed_uses_third_account_as_destination():
    data = transaction()
    seed = b"funding-seed"
    data["transaction"]["message"]["instructions"] = [
        instruction(
            SYSTEM,
            [PAYER, DELEGATE, DESTINATION],
            struct.pack("<IQQ", 11, 7, len(seed)) + seed + bytes(32),
        )
    ]
    fact = extract(data).transfers[0]
    assert fact.source_account == PAYER and fact.destination_account == DESTINATION
    assert fact.authority == DELEGATE and fact.amount_raw == "7"


@pytest.mark.parametrize("checked", [False, True])
def test_unrecorded_runtime_cpi_cannot_establish_snapshot_ownership(checked):
    data = token_metadata(transaction())
    data["transaction"]["message"]["instructions"] = [
        instruction(PARENT, [], b"\1"),
        token_transfer(7, checked=checked),
    ]
    data["meta"]["logMessages"] = [
        f"Program {PARENT} invoke [1]",
        f"Program {TOKEN} invoke [2]",
        f"Program {TOKEN} success",
        f"Program {PARENT} success",
        f"Program {TOKEN} invoke [1]",
        f"Program {TOKEN} success",
    ]
    result = extract(data)
    assert not result.complete and "inner_execution_unverified" in result.warnings
    assert all(info.owner is None for info in result.token_accounts)
    if checked:
        assert len(result.transfers) == 1
        assert result.transfers[0].source_owner is None
        assert result.transfers[0].destination_owner is None
        assert result.transfers[0].mint == MINT
    else:
        assert result.transfers == ()
        assert "transfer_mint_context_changed" in result.warnings


def test_balance_deltas_alone_do_not_create_inferred_transfer_pairs():
    data = transaction()
    data["meta"]["postBalances"][0] -= 700
    data["meta"]["postBalances"][1] += 700
    result = extract(data)
    assert result.transfers == ()
    assert len(result.native_balance_changes) == 2
    assert not result.complete and result.warnings


@pytest.mark.parametrize("amount,source,destination", [(0, PAYER, DESTINATION), (9, PAYER, PAYER)])
def test_zero_and_self_native_transfers_do_not_produce_funding_facts(amount, source, destination):
    data = transaction()
    data["transaction"]["message"]["instructions"] = [system_transfer(amount, source, destination)]
    assert extract(data).transfers == ()


def test_failed_transaction_has_no_transfer_facts_but_keeps_observed_fee_changes():
    data = transaction(error={"InstructionError": [0, "Custom"]})
    data["transaction"]["message"]["instructions"] = [system_transfer(700)]
    result = extract(data)
    assert result.transfers == ()
    assert result.native_balance_changes[0].delta_raw == "-5000"


def test_missing_header_does_not_infer_signers_from_signature_count():
    data = transaction()
    del data["transaction"]["message"]["header"]
    result = extract(data)
    assert result.signers is None and result.warnings and not result.complete


@pytest.mark.parametrize("checked", [False, True])
def test_spl_transfer_attributes_owners_from_balances_instead_of_delegate_authority(checked):
    data = token_metadata(transaction())
    data["transaction"]["message"]["instructions"] = [token_transfer(7, checked=checked)]
    result = extract(data)
    fact = result.transfers[0]
    assert fact.asset == "token" and fact.program_id == TOKEN
    assert fact.source_account == SOURCE_TOKEN and fact.destination_account == DESTINATION_TOKEN
    assert fact.source_owner == SOURCE_OWNER and fact.destination_owner == DESTINATION_OWNER
    assert fact.authority == DELEGATE and fact.source_owner != fact.authority
    assert fact.amount_raw == "7" and fact.net_amount_raw == "7"
    assert fact.mint == MINT and fact.decimals == 6
    changes = {change.account: change for change in result.token_balance_changes}
    assert changes[SOURCE_TOKEN].delta_raw == "-7"
    assert changes[DESTINATION_TOKEN].delta_raw == "7"
    assert changes[SOURCE_TOKEN].owner == SOURCE_OWNER


def test_checked_transfer_knows_mint_and_amount_without_owner_metadata():
    data = transaction()
    data["transaction"]["message"]["instructions"] = [token_transfer(7, checked=True)]
    result = extract(data)
    fact = result.transfers[0]
    assert fact.mint == MINT and fact.amount_raw == "7" and fact.decimals == 6
    assert fact.source_owner is None and fact.destination_owner is None
    assert fact.authority == DELEGATE


def test_unchecked_transfer_without_mint_metadata_is_incomplete_and_has_no_fact():
    data = transaction()
    data["transaction"]["message"]["instructions"] = [token_transfer(7)]
    result = extract(data)
    assert result.transfers == () and not result.complete and result.warnings


@pytest.mark.parametrize("field,value", [("mint", address(90)), ("programId", TOKEN_2022)])
def test_inconsistent_token_metadata_cannot_create_owner_or_mint_evidence(field, value):
    data = token_metadata(transaction())
    data["transaction"]["message"]["instructions"] = [token_transfer(7)]
    data["meta"]["postTokenBalances"][0][field] = value
    result = extract(data)
    assert not result.complete and result.warnings
    assert not result.transfers or result.transfers[0].source_owner is None


def test_changed_token_owner_is_unknown_and_is_never_replaced_by_authority():
    data = token_metadata(transaction())
    data["transaction"]["message"]["instructions"] = [token_transfer(7, checked=True)]
    data["meta"]["postTokenBalances"][0]["owner"] = address(91)
    result = extract(data)
    assert result.transfers[0].source_owner is None
    assert result.transfers[0].authority == DELEGATE
    assert not result.complete and result.warnings


@pytest.mark.parametrize("missing_side", ["preTokenBalances", "postTokenBalances"])
def test_owner_missing_from_one_existing_snapshot_cannot_prove_stable_owner(missing_side):
    data = token_metadata(transaction())
    data["transaction"]["message"]["instructions"] = [token_transfer(7, checked=True)]
    del data["meta"][missing_side][0]["owner"]
    result = extract(data)
    assert result.transfers[0].source_owner is None
    assert result.transfers[0].authority == DELEGATE
    assert not result.complete and result.warnings


@pytest.mark.parametrize("checked", [False, True])
def test_close_then_initialize_prevents_using_matching_boundary_mint_as_stable_context(checked):
    data = token_metadata(transaction())
    data["transaction"]["message"]["instructions"] = [
        instruction(TOKEN, [SOURCE_TOKEN, SOURCE_OWNER, DELEGATE], b"\x09"),
        instruction(TOKEN, [SOURCE_TOKEN, MINT], b"\x12" + (6).to_bytes(32, "big")),
        token_transfer(7, checked=checked),
    ]
    result = extract(data)
    assert not result.complete and result.warnings
    if checked:
        assert not result.transfers or result.transfers[0].source_owner is None
        if result.transfers:
            assert result.transfers[0].mint == MINT
    else:
        assert result.transfers == ()
        assert "transfer_mint_context_changed" in result.warnings


@pytest.mark.parametrize("missing_side", ["preTokenBalances", "postTokenBalances"])
def test_one_sided_token_snapshot_preserves_unknown_other_side(missing_side):
    data = token_metadata(transaction())
    data["meta"][missing_side] = []
    result = extract(data)
    changes = {change.account: change for change in result.token_balance_changes}
    change = changes[DESTINATION_TOKEN]
    assert change.delta_raw is None
    if missing_side == "preTokenBalances":
        assert change.pre_amount_raw is None and change.post_amount_raw == "17"
    else:
        assert change.pre_amount_raw == "10" and change.post_amount_raw is None
    assert not result.complete and result.warnings


def test_absent_token_balance_arrays_are_not_zero_balance_evidence():
    data = transaction()
    del data["meta"]["preTokenBalances"]
    del data["meta"]["postTokenBalances"]
    result = extract(data)
    assert result.token_balance_changes == ()
    assert not result.complete and result.warnings


def test_token_2022_ordinary_transfer_does_not_infer_net_or_fee_from_balance_delta():
    data = token_metadata(transaction(), program=TOKEN_2022, fee=2)
    data["transaction"]["message"]["instructions"] = [
        token_transfer(7, checked=True, program=TOKEN_2022)
    ]
    fact = extract(data).transfers[0]
    assert fact.amount_raw == "7"
    assert fact.net_amount_raw is None and fact.fee_amount_raw is None
    assert fact.source_owner == SOURCE_OWNER and fact.destination_owner == DESTINATION_OWNER


def test_token_2022_explicit_transfer_checked_with_fee_records_gross_net_and_fee():
    data = token_metadata(transaction(), program=TOKEN_2022, fee=2)
    data["transaction"]["message"]["instructions"] = [
        instruction(
            TOKEN_2022,
            [SOURCE_TOKEN, MINT, DESTINATION_TOKEN, DELEGATE],
            b"\x1a\x01" + struct.pack("<QBQ", 7, 6, 2),
        )
    ]
    fact = extract(data).transfers[0]
    assert fact.amount_raw == "7" and fact.net_amount_raw == "5" and fact.fee_amount_raw == "2"
    assert fact.decimals == 6 and fact.mint == MINT


@pytest.mark.parametrize("amount", [0, 7])
def test_self_or_zero_token_transfer_has_no_transfer_fact(amount):
    data = token_metadata(transaction(), amount=0)
    accounts = [SOURCE_TOKEN, MINT, SOURCE_TOKEN if amount else DESTINATION_TOKEN, DELEGATE]
    data["transaction"]["message"]["instructions"] = [
        instruction(TOKEN, accounts, b"\x0c" + struct.pack("<QB", amount, 6))
    ]
    assert extract(data).transfers == ()


def test_verified_inner_transfer_preserves_top_and_inner_instruction_indices():
    data = inner_transaction([system_transfer(9, stack_height=2)])
    result = extract(data)
    fact = result.transfers[0]
    assert fact.top_level_instruction_index == 0 and fact.inner_instruction_index == 0
    assert fact.stack_height == 2 and fact.amount_raw == "9"


@pytest.mark.parametrize("logs", [None, [], ["Program log: transfer succeeded"]])
def test_missing_inner_runtime_evidence_skips_inner_facts_and_marks_incomplete(logs):
    data = inner_transaction([system_transfer(9, stack_height=2)])
    data["meta"]["logMessages"] = logs
    result = extract(data)
    assert result.transfers == () and result.warnings and not result.complete


def test_caught_inner_failure_never_creates_transfer_fact():
    data = inner_transaction(
        [system_transfer(9, stack_height=2)],
        logs=[
            f"Program {PARENT} invoke [1]",
            f"Program {SYSTEM} invoke [2]",
            f"Program {SYSTEM} failed: custom program error: 0x1",
            f"Program {PARENT} success",
        ],
    )
    result = extract(data)
    assert result.transfers == () and result.warnings and not result.complete


def test_successful_child_of_caught_failed_parent_is_not_a_committed_transfer():
    inner = [
        instruction(HOOK, [], b"\x01", stack_height=2),
        system_transfer(9, stack_height=3),
    ]
    data = inner_transaction(
        inner,
        logs=[
            f"Program {PARENT} invoke [1]",
            f"Program {HOOK} invoke [2]",
            f"Program {SYSTEM} invoke [3]",
            f"Program {SYSTEM} success",
            f"Program {HOOK} failed: custom program error: 0x1",
            f"Program {PARENT} success",
        ],
    )
    result = extract(data)
    assert result.transfers == () and result.warnings and not result.complete


def test_truncated_parent_runtime_scope_cannot_commit_inner_fact():
    data = inner_transaction(
        [system_transfer(9, stack_height=2)],
        logs=[
            f"Program {PARENT} invoke [1]",
            f"Program {SYSTEM} invoke [2]",
            f"Program {SYSTEM} success",
        ],
    )
    result = extract(data)
    assert result.transfers == () and result.warnings and not result.complete


def test_unverified_inner_instruction_does_not_erase_valid_top_level_fact():
    data = transaction()
    data["transaction"]["message"]["instructions"] = [
        system_transfer(7),
        instruction(PARENT, [], b"\x01"),
    ]
    data["meta"]["innerInstructions"] = [
        {"index": 1, "instructions": [system_transfer(9, stack_height=2)]}
    ]
    data["meta"]["logMessages"] = None
    data["meta"]["postBalances"][0] -= 7
    data["meta"]["postBalances"][1] += 7
    result = extract(data)
    assert len(result.transfers) == 1
    assert result.transfers[0].inner_instruction_index is None
    assert result.transfers[0].amount_raw == "7"
    assert not result.complete and result.warnings


def test_missing_cpi_context_prevents_claiming_owners_for_valid_checked_top_level_transfer():
    data = token_metadata(transaction())
    data["transaction"]["message"]["instructions"] = [
        token_transfer(7, checked=True),
        instruction(PARENT, [], b"\x01"),
    ]
    data["meta"]["innerInstructions"] = [
        {"index": 1, "instructions": [instruction(HOOK, [], b"\x01", stack_height=2)]}
    ]
    data["meta"]["logMessages"] = None
    result = extract(data)
    assert len(result.transfers) == 1
    fact = result.transfers[0]
    assert fact.amount_raw == "7" and fact.mint == MINT
    assert fact.source_owner is None and fact.destination_owner is None
    assert not result.complete and result.warnings


def test_token_2022_nested_transfer_hook_correlates_original_inner_instruction():
    inner = [
        token_transfer(7, checked=True, program=TOKEN_2022, stack_height=2),
        instruction(HOOK, [], b"\x01", stack_height=3),
    ]
    data = token_metadata(
        inner_transaction(
            inner,
            logs=[
                f"Program {PARENT} invoke [1]",
                f"Program {TOKEN_2022} invoke [2]",
                f"Program {HOOK} invoke [3]",
                f"Program {HOOK} success",
                f"Program {TOKEN_2022} success",
                f"Program {PARENT} success",
            ],
        ),
        program=TOKEN_2022,
    )
    fact = extract(data).transfers[0]
    assert fact.inner_instruction_index == 0 and fact.stack_height == 2
    assert fact.program_id == TOKEN_2022 and fact.amount_raw == "7"


def test_runtime_log_labels_cannot_spoof_inner_success():
    data = inner_transaction(
        [system_transfer(9, stack_height=2)],
        logs=[
            f"Program {PARENT} invoke [1]",
            f"Program log: Program {SYSTEM} invoke [2]",
            f"Program log: Program {SYSTEM} success",
            f"Program {PARENT} success",
        ],
    )
    result = extract(data)
    assert result.transfers == () and result.warnings and not result.complete


def test_ata_rent_transfer_preserves_account_creation_context_without_wallet_funding():
    data = transaction()
    data["transaction"]["message"]["instructions"] = [
        instruction(
            ATA,
            [PAYER, DESTINATION_TOKEN, DESTINATION_OWNER, MINT, SYSTEM, TOKEN],
            b"\x01",
        )
    ]
    data["meta"]["innerInstructions"] = [
        {
            "index": 0,
            "instructions": [system_transfer(100, PAYER, DESTINATION_TOKEN, stack_height=2)],
        }
    ]
    data["meta"]["logMessages"] = [
        f"Program {ATA} invoke [1]",
        f"Program {SYSTEM} invoke [2]",
        f"Program {SYSTEM} success",
        f"Program {ATA} success",
    ]
    data["meta"]["postTokenBalances"] = [
        token_balance(data, DESTINATION_TOKEN, 0, owner=DESTINATION_OWNER)
    ]
    fact = extract(data).transfers[0]
    assert fact.context == "account_creation"
    assert fact.destination_account == DESTINATION_TOKEN
    assert fact.destination_account != DESTINATION_OWNER
    assert fact.destination_owner is None


@pytest.mark.parametrize("creation_data", [b"", b"\x00", b"\x01"])
def test_successful_ata_creation_roles_resolve_new_token_destination_owner(creation_data):
    data = transaction()
    data["transaction"]["message"]["instructions"] = [
        instruction(
            ATA,
            [PAYER, DESTINATION_TOKEN, DESTINATION_OWNER, MINT, SYSTEM, TOKEN],
            creation_data,
        ),
        token_transfer(7),
    ]
    data["meta"]["preTokenBalances"] = [token_balance(data, SOURCE_TOKEN, 100, owner=SOURCE_OWNER)]
    data["meta"]["postTokenBalances"] = [
        token_balance(data, SOURCE_TOKEN, 93, owner=SOURCE_OWNER),
        token_balance(data, DESTINATION_TOKEN, 7, owner=DESTINATION_OWNER),
    ]
    result = extract(data)
    assert len(result.transfers) == 1
    fact = result.transfers[0]
    assert fact.asset == "token" and fact.destination_owner == DESTINATION_OWNER
    assert fact.destination_account == DESTINATION_TOKEN
    assert fact.source_owner == SOURCE_OWNER and fact.mint == MINT
    change = next(
        change for change in result.token_balance_changes if change.account == DESTINATION_TOKEN
    )
    assert change.pre_amount_raw is None and change.delta_raw is None


@pytest.mark.parametrize(
    "field,value",
    [("owner", address(91)), ("mint", address(92)), ("programId", TOKEN_2022), ("decimals", 8)],
)
def test_ata_roles_do_not_refill_conflicting_token_metadata(field, value):
    data = token_metadata(transaction())
    data["transaction"]["message"]["instructions"] = [
        instruction(ATA, [PAYER, SOURCE_TOKEN, SOURCE_OWNER, MINT, SYSTEM, TOKEN], b"\x01"),
        token_transfer(7, checked=True),
    ]
    changed = data["meta"]["postTokenBalances"][0]
    if field == "decimals":
        changed["uiTokenAmount"][field] = value
    else:
        changed[field] = value
    result = extract(data)
    assert not result.complete and result.warnings
    if field == "owner":
        assert not result.transfers or result.transfers[0].source_owner is None
    else:
        assert result.transfers == ()


def test_unrelated_top_level_sol_donation_to_created_ata_keeps_transfer_context():
    data = transaction()
    data["transaction"]["message"]["instructions"] = [
        instruction(
            ATA, [PAYER, DESTINATION_TOKEN, DESTINATION_OWNER, MINT, SYSTEM, TOKEN], b"\x01"
        ),
        system_transfer(9, PAYER, DESTINATION_TOKEN),
    ]
    data["meta"]["postBalances"][0] -= 9
    data["meta"]["postBalances"][KEYS.index(DESTINATION_TOKEN)] += 9
    fact = extract(data).transfers[0]
    assert fact.context == "transfer"
    assert fact.destination_account == DESTINATION_TOKEN and fact.destination_owner is None


def test_system_create_account_does_not_turn_rent_into_wallet_transfer():
    data = transaction()
    data["transaction"]["message"]["instructions"] = [
        instruction(
            SYSTEM,
            [PAYER, DESTINATION_TOKEN],
            struct.pack("<IQQ", 0, 100, 165) + bytes(32),
        )
    ]
    data["meta"]["postBalances"][0] -= 100
    data["meta"]["postBalances"][KEYS.index(DESTINATION_TOKEN)] += 100
    result = extract(data)
    assert result.transfers == ()
    assert len(result.native_balance_changes) == 2


@pytest.mark.parametrize("tag", [7, 8, 9])
def test_mint_burn_and_close_are_not_reinterpreted_as_transfers(tag):
    data = token_metadata(transaction())
    data["transaction"]["message"]["instructions"] = [
        instruction(TOKEN, [MINT, SOURCE_TOKEN, DELEGATE], bytes([tag]) + struct.pack("<Q", 7))
    ]
    result = extract(data)
    assert result.transfers == () and result.token_balance_changes


def test_v0_loaded_addresses_preserve_static_writable_readonly_account_order():
    keys = [PAYER, DELEGATE, MINT, TOKEN]
    resolved = keys + [SOURCE_TOKEN, DESTINATION_TOKEN] + [SOURCE_OWNER, DESTINATION_OWNER]
    data = transaction(keys=keys)
    data["version"] = 0
    data["transaction"]["message"]["addressTableLookups"] = [
        {"accountKey": address(20), "writableIndexes": [0, 1], "readonlyIndexes": [2, 3]}
    ]
    data["meta"]["loadedAddresses"] = {
        "writable": [SOURCE_TOKEN, DESTINATION_TOKEN],
        "readonly": [SOURCE_OWNER, DESTINATION_OWNER],
    }
    data["meta"]["preBalances"] = [1_000_000] * len(resolved)
    data["meta"]["postBalances"] = [995_000] + [1_000_000] * (len(resolved) - 1)
    token_metadata(data)
    data["transaction"]["message"]["instructions"] = [
        token_transfer(7, checked=True, keys=resolved)
    ]
    result = extract(data)
    assert result.account_keys == tuple(resolved)
    fact = result.transfers[0]
    assert fact.source_account == SOURCE_TOKEN and fact.destination_account == DESTINATION_TOKEN


def test_v1_inline_transaction_config_preserves_transfer_facts():
    data = transaction()
    data["version"] = 1
    data["transaction"]["message"]["transactionConfig"] = {
        "computeUnitLimit": 30000,
        "heapSize": None,
        "loadedAccountsDataSizeLimit": 200000,
        "priorityFee": 9007199254740993,
    }
    data["transaction"]["message"]["instructions"] = [system_transfer(7)]
    assert extract(data).transfers[0].amount_raw == "7"


def test_extraction_does_not_mutate_journaled_payload_or_input_dictionary():
    data = token_metadata(transaction())
    data["transaction"]["message"]["instructions"] = [token_transfer(7)]
    original = copy.deepcopy(data)
    record = history(data)
    payload_json = record.payload_json
    extractor = TransferExtractor()
    first = extractor.extract(record)
    second = extractor.extract(record)
    assert first == second and record.payload_json == payload_json
    assert json.loads(record.payload_json) == original and data == original
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        first.signature = "changed"


def test_fact_identity_is_instruction_based_and_independent_of_replay_raw_row():
    data = transaction()
    data["transaction"]["message"]["instructions"] = [system_transfer(7), system_transfer(7)]
    first = extract(data)
    replay = extract(data, raw_event_id=100, page_raw_event_id=200)
    keys = [fact.fact_key for fact in first.transfers]
    assert len(set(keys)) == 2
    assert keys == [fact.fact_key for fact in replay.transfers]
    assert replay.transfers[0].raw_event_id == 100


@pytest.mark.parametrize("field,value", [("slot", 99), ("signature", base58(bytes([2]) * 64))])
def test_history_identity_mismatch_is_error_without_transfer_facts(field, value):
    data = transaction()
    data["transaction"]["message"]["instructions"] = [system_transfer(7)]
    record = history(data).model_copy(update={field: value})
    result = TransferExtractor().extract(record)
    assert result.state is EvidenceState.ERROR and result.transfers == ()
    assert not result.complete and result.warnings


@pytest.mark.parametrize("data", ["{", "[]", "null", '{"untrusted":"arbitrary secret"}'])
def test_invalid_journal_payload_is_fixed_error_and_never_leaks_content(data):
    record = history(transaction()).model_copy(update={"payload_json": data})
    result = TransferExtractor().extract(record)
    assert result.state is EvidenceState.ERROR and result.transfers == ()
    assert not result.complete and result.warnings
    assert "arbitrary secret" not in str(result.warnings)


@pytest.mark.parametrize("field", ["fee", "preBalances", "postBalances", "err"])
def test_unavailable_native_metadata_is_error_without_inferred_transfer_facts(field):
    data = transaction()
    record = history(data)
    del data["meta"][field]
    record = record.model_copy(update={"payload_json": json.dumps(data)})
    result = TransferExtractor().extract(record)
    assert result.state is EvidenceState.ERROR and result.transfers == ()
    assert not result.complete and result.warnings


@pytest.mark.parametrize("raw_event_id,page_raw_event_id", [(0, 2), (1, 0), (True, 2), (1, None)])
def test_invalid_journal_references_are_not_returned_as_observed_facts(
    raw_event_id, page_raw_event_id
):
    record = history(transaction()).model_copy(
        update={"raw_event_id": raw_event_id, "page_raw_event_id": page_raw_event_id}
    )
    result = TransferExtractor().extract(record)
    assert result.state is EvidenceState.ERROR and result.transfers == ()


@pytest.mark.parametrize("record", [None, {}, [], "secret", object()])
def test_arbitrary_input_returns_fixed_error_instead_of_raising(record):
    result = TransferExtractor().extract(record)
    assert result.state is EvidenceState.ERROR and result.transfers == ()
    assert not result.complete and result.warnings
    assert "secret" not in str(result.warnings)


def test_duplicate_json_keys_are_not_silently_overwritten():
    record = history(transaction())
    payload = record.payload_json.replace('"slot":300000007', '"slot":99,"slot":300000007')
    result = TransferExtractor().extract(record.model_copy(update={"payload_json": payload}))
    assert result.state is EvidenceState.ERROR and result.transfers == ()


def test_duplicate_token_balance_rows_do_not_supply_trusted_owner_evidence():
    data = token_metadata(transaction())
    data["transaction"]["message"]["instructions"] = [token_transfer(7, checked=True)]
    data["meta"]["preTokenBalances"].append(copy.deepcopy(data["meta"]["preTokenBalances"][0]))
    data["meta"]["postTokenBalances"].append(copy.deepcopy(data["meta"]["postTokenBalances"][0]))
    result = extract(data)
    assert not result.complete and result.warnings
    assert not result.transfers or result.transfers[0].source_owner is None


@pytest.mark.parametrize("suffix", [b"\x00", b"extra"])
def test_unknown_transfer_binary_trailing_layout_is_not_guessed(suffix):
    data = transaction()
    data["transaction"]["message"]["instructions"] = [
        instruction(SYSTEM, [PAYER, DESTINATION], struct.pack("<IQ", 2, 7) + suffix)
    ]
    result = extract(data)
    assert result.transfers == () and result.warnings and not result.complete


@pytest.mark.parametrize("encoded", ["0", "!", "", "1"])
def test_invalid_or_truncated_transfer_data_cannot_create_facts(encoded):
    data = transaction()
    item = system_transfer(7)
    item["data"] = encoded
    data["transaction"]["message"]["instructions"] = [item]
    result = extract(data)
    assert result.transfers == () and result.warnings and not result.complete
