"""Golden binary transfer layouts and bounded, conservative rejection behavior."""

from __future__ import annotations

import struct
from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from app.config import BASE58_ALPHABET, _validate_base58_address
from app.transfer_instructions import (
    ASSOCIATED_TOKEN_PROGRAM,
    SYSTEM_PROGRAM,
    TOKEN_2022_PROGRAM,
    TOKEN_PROGRAM,
    DecodedTransferInstruction,
    TransferInstructionError,
    decode_transfer_instruction,
    instruction_bytes,
)


def base58(data: bytes) -> str:
    number = int.from_bytes(data, "big")
    digits: list[str] = []
    while number:
        number, digit = divmod(number, 58)
        digits.append(BASE58_ALPHABET[digit])
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + "".join(reversed(digits))


SOURCE = base58(b"\x01" * 32)
DESTINATION = base58(b"\x02" * 32)
MINT = base58(b"\x03" * 32)
AUTHORITY = base58(b"\x04" * 32)
EXTRA = base58(b"\x05" * 32)
U64_MAX = 2**64 - 1
LAYOUTS = [
    (SYSTEM_PROGRAM, "Transfer"),
    (SYSTEM_PROGRAM, "TransferWithSeed"),
    (TOKEN_PROGRAM, "Transfer"),
    (TOKEN_PROGRAM, "TransferChecked"),
    (TOKEN_2022_PROGRAM, "Transfer"),
    (TOKEN_2022_PROGRAM, "TransferChecked"),
    (TOKEN_2022_PROGRAM, "TransferCheckedWithFee"),
]


def fixture(
    program: str,
    name: str,
    *,
    amount: int = 2**53 + 1,
    decimals: int = 9,
    fee: int = 7,
    seed: bytes = "🧪seed".encode(),
) -> tuple[list[str], bytes]:
    """Encode known test vectors; production code never constructs transactions."""
    if program == SYSTEM_PROGRAM:
        if name == "Transfer":
            return [SOURCE, DESTINATION], struct.pack("<IQ", 2, amount)
        return (
            [SOURCE, AUTHORITY, DESTINATION],
            struct.pack("<IQQ", 11, amount, len(seed)) + seed + b"\x06" * 32,
        )
    if name == "Transfer":
        return [SOURCE, DESTINATION, AUTHORITY], struct.pack("<BQ", 3, amount)
    if name == "TransferChecked":
        return [SOURCE, MINT, DESTINATION, AUTHORITY], struct.pack("<BQB", 12, amount, decimals)
    return (
        [SOURCE, MINT, DESTINATION, AUTHORITY],
        struct.pack("<BBQBQ", 26, 1, amount, decimals, fee),
    )


def decode(program: str, name: str, **kwargs: Any) -> DecodedTransferInstruction:
    accounts, data = fixture(program, name, **kwargs)
    result = decode_transfer_instruction(program, accounts, base58(data))
    assert result is not None
    return result


def assert_error(program: str, accounts: Any, data: Any, code: str) -> None:
    with pytest.raises(TransferInstructionError) as error:
        decode_transfer_instruction(program, accounts, data)
    assert error.value.code == code
    assert str(error.value) == "transfer instruction could not be decoded"
    assert error.value.retryable is False


@pytest.mark.parametrize("program,name", LAYOUTS)
def test_supported_golden_layout_preserves_roles_and_amounts(program: str, name: str) -> None:
    result = decode(program, name)
    is_token = program != SYSTEM_PROGRAM
    checked = "Checked" in name
    expected_fee = "7" if name == "TransferCheckedWithFee" else "0"
    expected_net = str(2**53 + 1 - int(expected_fee))
    if program == TOKEN_2022_PROGRAM and name != "TransferCheckedWithFee":
        expected_fee = None
        expected_net = None
    assert result == DecodedTransferInstruction(
        asset="token" if is_token else "sol",
        instruction_name=name,
        program_id=program,
        source_account=SOURCE,
        destination_account=DESTINATION,
        mint=MINT if checked else None,
        authority=SOURCE if program == SYSTEM_PROGRAM and name == "Transfer" else AUTHORITY,
        amount_raw=str(2**53 + 1),
        decimals=9 if checked else None,
        fee_amount_raw=expected_fee,
        net_amount_raw=expected_net,
    )


@pytest.mark.parametrize("program,name", LAYOUTS)
@pytest.mark.parametrize("amount", [0, 1, 2**53 + 1, U64_MAX])
def test_u64_values_remain_exact_decimal_strings(program: str, name: str, amount: int) -> None:
    result = decode(program, name, amount=amount, fee=0)
    assert result.amount_raw == str(amount)
    if result.net_amount_raw is not None:
        assert result.net_amount_raw == str(amount)


@pytest.mark.parametrize("program,name", LAYOUTS)
def test_every_truncated_supported_layout_is_rejected(program: str, name: str) -> None:
    accounts, data = fixture(program, name)
    for end in range(len(data)):
        assert_error(program, accounts, base58(data[:end]), "malformed_transfer_instruction")


@pytest.mark.parametrize("program,name", LAYOUTS)
@pytest.mark.parametrize("suffix", [b"\0", b"unexpected"])
def test_trailing_bytes_cannot_be_accepted_as_known_layout(
    program: str, name: str, suffix: bytes
) -> None:
    accounts, data = fixture(program, name)
    assert_error(program, accounts, base58(data + suffix), "malformed_transfer_instruction")


@pytest.mark.parametrize("program,name", LAYOUTS)
def test_additional_multisig_or_hook_accounts_do_not_change_roles(program: str, name: str) -> None:
    accounts, data = fixture(program, name)
    expected = decode_transfer_instruction(program, accounts, base58(data))
    assert decode_transfer_instruction(program, [*accounts, EXTRA, EXTRA], base58(data)) == expected


@pytest.mark.parametrize("program,name", LAYOUTS)
def test_missing_required_account_roles_are_rejected(program: str, name: str) -> None:
    accounts, data = fixture(program, name)
    for end in range(len(accounts)):
        assert_error(program, accounts[:end], base58(data), "invalid_transfer_accounts")


@pytest.mark.parametrize("accounts", [None, SOURCE, b"bytes", [], [SOURCE, None], [SOURCE, "0bad"]])
def test_malformed_accounts_return_only_fixed_diagnostics(accounts: Any) -> None:
    assert_error(
        SYSTEM_PROGRAM, accounts, base58(struct.pack("<IQ", 2, 5)), "invalid_transfer_accounts"
    )


def test_extra_accounts_are_also_validated() -> None:
    accounts, data = fixture(TOKEN_PROGRAM, "TransferChecked")
    assert_error(
        TOKEN_PROGRAM, [*accounts, "secret-value"], base58(data), "invalid_transfer_accounts"
    )


def test_account_resource_limit_is_bounded() -> None:
    data = base58(struct.pack("<IQ", 2, 1))
    assert decode_transfer_instruction(SYSTEM_PROGRAM, [SOURCE] * 256, data) is not None
    assert_error(SYSTEM_PROGRAM, [SOURCE] * 257, data, "invalid_transfer_accounts")


@pytest.mark.parametrize("seed", [b"", b"plain", "é🚀".encode()])
def test_seed_length_is_utf8_byte_length(seed: bytes) -> None:
    result = decode(SYSTEM_PROGRAM, "TransferWithSeed", seed=seed)
    assert result.source_account == SOURCE
    assert result.destination_account == DESTINATION
    assert result.authority == AUTHORITY


def test_invalid_utf8_seed_is_rejected() -> None:
    accounts, data = fixture(SYSTEM_PROGRAM, "TransferWithSeed", seed=b"\xff")
    assert_error(SYSTEM_PROGRAM, accounts, base58(data), "malformed_transfer_instruction")


@pytest.mark.parametrize("claimed_length", [0, 3, U64_MAX])
def test_seed_length_mismatch_is_rejected_without_allocating(claimed_length: int) -> None:
    accounts, data = fixture(SYSTEM_PROGRAM, "TransferWithSeed")
    invalid = data[:12] + struct.pack("<Q", claimed_length) + data[20:]
    assert_error(SYSTEM_PROGRAM, accounts, base58(invalid), "malformed_transfer_instruction")


@pytest.mark.parametrize("program", [TOKEN_PROGRAM, TOKEN_2022_PROGRAM])
@pytest.mark.parametrize("decimals", [0, 255])
def test_checked_decimals_accept_exact_u8_boundaries(program: str, decimals: int) -> None:
    assert decode(program, "TransferChecked", decimals=decimals).decimals == decimals


@pytest.mark.parametrize("amount,fee", [(0, 0), (1, 0), (1, 1), (U64_MAX, U64_MAX), (U64_MAX, 0)])
def test_token_2022_explicit_fee_preserves_u64_and_net_amount(amount: int, fee: int) -> None:
    result = decode(TOKEN_2022_PROGRAM, "TransferCheckedWithFee", amount=amount, fee=fee)
    assert result.fee_amount_raw == str(fee)
    assert result.net_amount_raw == str(amount - fee)


def test_explicit_fee_greater_than_amount_is_rejected() -> None:
    accounts, data = fixture(TOKEN_2022_PROGRAM, "TransferCheckedWithFee", amount=3, fee=4)
    assert_error(TOKEN_2022_PROGRAM, accounts, base58(data), "invalid_transfer_fee")


@pytest.mark.parametrize("name", ["Transfer", "TransferChecked"])
def test_token_2022_fee_and_net_remain_unknown_without_mint_state(name: str) -> None:
    result = decode(TOKEN_2022_PROGRAM, name)
    assert result.fee_amount_raw is None
    assert result.net_amount_raw is None


@pytest.mark.parametrize("program,name", LAYOUTS)
def test_self_transfer_is_only_an_instruction_argument(program: str, name: str) -> None:
    accounts, data = fixture(program, name)
    destination_index = 1 if name == "Transfer" else 2
    accounts[destination_index] = SOURCE
    result = decode_transfer_instruction(program, accounts, base58(data))
    assert result is not None
    assert result.source_account == result.destination_account == SOURCE
    assert result.amount_raw == str(2**53 + 1)
    assert "does not assert balance credit" in (DecodedTransferInstruction.__doc__ or "")


@pytest.mark.parametrize(
    "program,data",
    [
        (SYSTEM_PROGRAM, struct.pack("<I", 0)),
        (SYSTEM_PROGRAM, struct.pack("<I", 0xFFFFFFFF)),
        (TOKEN_PROGRAM, b"\0"),
        (TOKEN_PROGRAM, b"\x1a\x01"),
        (TOKEN_2022_PROGRAM, b"\xff"),
        (TOKEN_2022_PROGRAM, b"\x1a\0"),
        (TOKEN_2022_PROGRAM, b"\x1a\x02"),
    ],
)
def test_unknown_known_program_instructions_are_explicitly_unsupported(
    program: str, data: bytes
) -> None:
    assert_error(program, [], base58(data), "unsupported_transfer_instruction")


@pytest.mark.parametrize("program", [SOURCE, ASSOCIATED_TOKEN_PROGRAM, "unknown"])
def test_unrelated_programs_do_not_inspect_data_or_accounts(program: str) -> None:
    assert decode_transfer_instruction(program, None, None) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "data", [None, 3, True, b"abc", "0", "O", "I", "l", "é", " 1", "1\n", "+", "/", "="]
)
def test_invalid_base58_returns_fixed_safe_error(data: Any) -> None:
    assert_error(SYSTEM_PROGRAM, [SOURCE, DESTINATION], data, "invalid_transfer_data")


@pytest.mark.parametrize("data", ["1" * 4097, "z" * 5594, "1" * 5595])
def test_encoded_or_decoded_data_resource_limits_are_bounded(data: str) -> None:
    assert_error(SYSTEM_PROGRAM, [SOURCE, DESTINATION], data, "transfer_instruction_resource_limit")


def test_exact_decoded_resource_boundary_reaches_layout_validation() -> None:
    assert_error(SYSTEM_PROGRAM, [], "1" * 4096, "unsupported_transfer_instruction")


def test_canonical_base58_leading_zero_bytes_are_preserved() -> None:
    # Adding a leading zero changes the discriminator, rather than being stripped.
    data = b"\0" + struct.pack("<IQ", 2, 1)
    assert_error(SYSTEM_PROGRAM, [], base58(data), "unsupported_transfer_instruction")


@pytest.mark.parametrize("data", [b"", b"\0", b"\0\0", b"\0hello", b"\xff" * 4096])
def test_public_byte_decoder_preserves_all_bytes_at_resource_boundary(data: bytes) -> None:
    assert instruction_bytes(base58(data)) == data


def test_all_exported_program_constants_are_valid_public_keys() -> None:
    for program in (SYSTEM_PROGRAM, TOKEN_PROGRAM, TOKEN_2022_PROGRAM, ASSOCIATED_TOKEN_PROGRAM):
        assert _validate_base58_address(program) == program


def test_decoded_instruction_is_immutable() -> None:
    result = decode(TOKEN_PROGRAM, "Transfer")
    with pytest.raises(FrozenInstanceError):
        result.amount_raw = "changed"  # type: ignore[misc]
