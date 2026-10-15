"""Read-only decoding of explicitly supported compiled transfer instructions."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.config import BASE58_ALPHABET, _validate_base58_address
from app.logging_config import SafeError

SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ASSOCIATED_TOKEN_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"

_MAX_INSTRUCTION_BYTES = 4096
_MAX_ENCODED_CHARACTERS = 5594
_MAX_ACCOUNTS = 256
_BASE58_DIGITS = {character: index for index, character in enumerate(BASE58_ALPHABET)}


class TransferInstructionError(SafeError):
    """A fixed diagnostic that never includes untrusted instruction data."""

    def __init__(self, code: str) -> None:
        super().__init__(code, "transfer instruction could not be decoded")


@dataclass(frozen=True, slots=True)
class DecodedTransferInstruction:
    """Instruction arguments and account roles; this does not assert balance credit."""

    asset: Literal["sol", "token"]
    instruction_name: str
    program_id: str
    source_account: str
    destination_account: str
    mint: str | None
    authority: str | None
    amount_raw: str
    decimals: int | None
    fee_amount_raw: str | None
    net_amount_raw: str | None


def instruction_bytes(data: str) -> bytes:
    """Decode canonical Base58, including empty data, within the resource bound."""
    if not isinstance(data, str):
        raise TransferInstructionError("invalid_transfer_data")
    if len(data) > _MAX_ENCODED_CHARACTERS:
        raise TransferInstructionError("transfer_instruction_resource_limit")
    number = 0
    for start in range(0, len(data), 8):
        chunk = data[start : start + 8]
        digit = 0
        for character in chunk:
            if character not in _BASE58_DIGITS:
                raise TransferInstructionError("invalid_transfer_data")
            digit = digit * 58 + _BASE58_DIGITS[character]
        number = number * 58 ** len(chunk) + digit
    leading_zeros = len(data) - len(data.lstrip("1"))
    size = (number.bit_length() + 7) // 8
    if size + leading_zeros > _MAX_INSTRUCTION_BYTES:
        raise TransferInstructionError("transfer_instruction_resource_limit")
    # The alphabet and exact count of leading zero bytes define canonical Base58.
    return b"\0" * leading_zeros + number.to_bytes(size, "big")


def _account_roles(accounts: Sequence[str], required: int) -> None:
    if (
        not isinstance(accounts, Sequence)
        or isinstance(accounts, (str, bytes, bytearray))
        or not required <= len(accounts) <= _MAX_ACCOUNTS
    ):
        raise TransferInstructionError("invalid_transfer_accounts")
    for account in accounts:
        if not isinstance(account, str):
            raise TransferInstructionError("invalid_transfer_accounts")
        try:
            _validate_base58_address(account)
        except ValueError:
            raise TransferInstructionError("invalid_transfer_accounts") from None


def _exact_size(data: bytes, expected: int) -> None:
    if len(data) != expected:
        raise TransferInstructionError("malformed_transfer_instruction")


def _system_transfer(accounts: Sequence[str], data: bytes) -> DecodedTransferInstruction:
    if len(data) < 4:
        raise TransferInstructionError("malformed_transfer_instruction")
    discriminator = int.from_bytes(data[:4], "little")
    if discriminator == 2:
        _exact_size(data, 12)
        _account_roles(accounts, 2)
        destination_index = 1
        authority_index = 0
        instruction_name = "Transfer"
    elif discriminator == 11:
        if len(data) < 52:
            raise TransferInstructionError("malformed_transfer_instruction")
        seed_length = int.from_bytes(data[12:20], "little")
        _exact_size(data, 52 + seed_length)
        try:
            data[20 : 20 + seed_length].decode("utf-8")
        except UnicodeDecodeError:
            raise TransferInstructionError("malformed_transfer_instruction") from None
        _account_roles(accounts, 3)
        destination_index = 2
        authority_index = 1
        instruction_name = "TransferWithSeed"
    else:
        raise TransferInstructionError("unsupported_transfer_instruction")
    amount = str(int.from_bytes(data[4:12], "little"))
    return DecodedTransferInstruction(
        asset="sol",
        instruction_name=instruction_name,
        program_id=SYSTEM_PROGRAM,
        source_account=accounts[0],
        destination_account=accounts[destination_index],
        mint=None,
        authority=accounts[authority_index],
        amount_raw=amount,
        decimals=None,
        fee_amount_raw="0",
        net_amount_raw=amount,
    )


def _token_transfer(
    program_id: str, accounts: Sequence[str], data: bytes
) -> DecodedTransferInstruction:
    if not data:
        raise TransferInstructionError("malformed_transfer_instruction")
    discriminator = data[0]
    fee_amount: str | None = "0" if program_id == TOKEN_PROGRAM else None
    decimals: int | None = None
    mint: str | None = None
    if discriminator == 3:
        _exact_size(data, 9)
        _account_roles(accounts, 3)
        destination_index = 1
        authority_index = 2
        amount_offset = 1
        instruction_name = "Transfer"
    elif discriminator == 12:
        _exact_size(data, 10)
        _account_roles(accounts, 4)
        destination_index = 2
        authority_index = 3
        amount_offset = 1
        mint = accounts[1]
        decimals = data[9]
        instruction_name = "TransferChecked"
    elif discriminator == 26 and program_id == TOKEN_2022_PROGRAM:
        if len(data) < 2:
            raise TransferInstructionError("malformed_transfer_instruction")
        if data[1] != 1:
            raise TransferInstructionError("unsupported_transfer_instruction")
        _exact_size(data, 19)
        _account_roles(accounts, 4)
        destination_index = 2
        authority_index = 3
        amount_offset = 2
        mint = accounts[1]
        decimals = data[10]
        fee_amount = str(int.from_bytes(data[11:19], "little"))
        instruction_name = "TransferCheckedWithFee"
    else:
        raise TransferInstructionError("unsupported_transfer_instruction")
    amount = int.from_bytes(data[amount_offset : amount_offset + 8], "little")
    if fee_amount is not None and int(fee_amount) > amount:
        raise TransferInstructionError("invalid_transfer_fee")
    net_amount = None if fee_amount is None else str(amount - int(fee_amount))
    return DecodedTransferInstruction(
        asset="token",
        instruction_name=instruction_name,
        program_id=program_id,
        source_account=accounts[0],
        destination_account=accounts[destination_index],
        mint=mint,
        authority=accounts[authority_index],
        amount_raw=str(amount),
        decimals=decimals,
        fee_amount_raw=fee_amount,
        net_amount_raw=net_amount,
    )


def decode_transfer_instruction(
    program_id: str, accounts: Sequence[str], data: str
) -> DecodedTransferInstruction | None:
    """Decode a supported layout without inferring owners or successful execution.

    Amounts describe the instruction, including self-transfers and zero amounts.
    Token authority accounts may be delegates or multisig authorities. Token-2022
    ordinary transfers leave fees and net amounts unknown without mint state.
    Unrelated programs are ignored without examining their payloads.
    """
    if program_id not in (SYSTEM_PROGRAM, TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        return None
    decoded = instruction_bytes(data)
    if program_id == SYSTEM_PROGRAM:
        return _system_transfer(accounts, decoded)
    return _token_transfer(program_id, accounts, decoded)
