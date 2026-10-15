"""Strict instruction decoding for documented full Parsed Streams notifications.

Parsed Streams supplies instructions, summaries and transfers, but its current
wire contract does not supply emitted Anchor events. This adapter never creates
an event from an instruction name or from a provider summary.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import Field, StrictBool, StrictStr, ValidationError, field_validator, model_validator

from app.config import BASE58_ALPHABET, Settings, _validate_base58_address
from app.db.catalog import load_pinned_catalog
from app.decoders import DecoderConfigurationError, DecodeResult, DecoderInput
from app.domain.enums import DecodeStatus, ProgramName
from app.domain.models import (
    DecodedAccount,
    DecodedInstruction,
    DomainModel,
    JsonObject,
    JsonValue,
    NonNegativeInt,
    _signature,
)
from app.integrations.parsed_stream_models import ProgramDescription

PositiveInt = Annotated[int, Field(strict=True, ge=1)]
RawAmount = NonNegativeInt | Annotated[str, Field(strict=True, pattern=r"^(0|[1-9][0-9]*)$")]


class _Summary(DomainModel):
    type: StrictStr
    description: StrictStr
    # The provider documents this as a protocol-specific payload, not a closed schema.
    parsedData: JsonObject | None = None


class _NativeTransfer(DomainModel):
    fromUserAccount: StrictStr
    toUserAccount: StrictStr
    amount: RawAmount

    _addresses = field_validator("fromUserAccount", "toUserAccount")(_validate_base58_address)


class _TokenTransfer(DomainModel):
    fromUserAccount: StrictStr
    toUserAccount: StrictStr
    fromTokenAccount: StrictStr
    toTokenAccount: StrictStr
    rawTokenAmount: RawAmount
    decimals: Annotated[int, Field(strict=True, ge=0, le=255)]
    tokenStandard: StrictStr
    mint: StrictStr

    _addresses = field_validator(
        "fromUserAccount", "toUserAccount", "fromTokenAccount", "toTokenAccount", "mint"
    )(_validate_base58_address)


class _Transaction(DomainModel):
    signature: StrictStr
    slot: NonNegativeInt
    blockTime: NonNegativeInt | None
    feePayer: StrictStr
    fee: NonNegativeInt
    accountKeys: list[StrictStr]
    status: Literal["ok", "error"]
    error: JsonObject | list[JsonValue] | None
    summary: _Summary | None
    nativeTransfers: list[_NativeTransfer]
    tokenTransfers: list[_TokenTransfer]

    _signature_check = field_validator("signature")(_signature)
    _payer_check = field_validator("feePayer")(_validate_base58_address)

    @field_validator("accountKeys")
    @classmethod
    def validate_keys(cls, value: list[str]) -> list[str]:
        for key in value:
            _validate_base58_address(key)
        return value

    @model_validator(mode="after")
    def validate_context(self) -> _Transaction:
        if not self.accountKeys or self.accountKeys[0] != self.feePayer:
            raise ValueError("fee payer must be the first account key")
        if (self.status == "ok") != (self.error is None):
            raise ValueError("transaction status and error disagree")
        return self


class _Account(DomainModel):
    name: StrictStr
    pubkey: StrictStr
    isSigner: StrictBool
    isWritable: StrictBool

    _address_check = field_validator("pubkey")(_validate_base58_address)


class _Decoded(DomainModel):
    args: JsonObject
    accounts: list[_Account]


class _Instruction(DomainModel):
    instructionIndex: NonNegativeInt
    innerInstructionIndex: NonNegativeInt | None
    stackHeight: PositiveInt
    programId: StrictStr
    programName: StrictStr | None
    instructionName: StrictStr | None
    summary: _Summary | None
    decoded: _Decoded | None
    rawData: StrictStr | None = None
    rawAccounts: list[StrictStr] | None = None

    _address_check = field_validator("programId")(_validate_base58_address)

    @model_validator(mode="after")
    def validate_shape(self) -> _Instruction:
        if self.innerInstructionIndex is None:
            if self.stackHeight != 1:
                raise ValueError("top-level instruction has invalid stack height")
        elif self.stackHeight < 2:
            raise ValueError("inner instruction has invalid stack height")
        if self.decoded is None:
            if self.rawData is None or self.rawAccounts is None:
                raise ValueError("undecoded instruction must retain bytes and accounts")
            if any(character not in BASE58_ALPHABET for character in self.rawData):
                raise ValueError("instruction bytes must be Base58")
            for address in self.rawAccounts:
                _validate_base58_address(address)
        elif (
            not self.programName
            or not self.instructionName
            or self.rawData is not None
            or self.rawAccounts is not None
        ):
            raise ValueError("decoded instruction has invalid identity or raw fields")
        return self


class _Value(DomainModel):
    transaction: _Transaction
    instructions: list[_Instruction]
    matchedIndexes: list[NonNegativeInt]

    @model_validator(mode="after")
    def validate_positions(self) -> _Value:
        if len(set(self.matchedIndexes)) != len(self.matchedIndexes) or any(
            index >= len(self.instructions) for index in self.matchedIndexes
        ):
            raise ValueError("matched instruction indices are invalid")
        top_index = -1
        inner_index = -1
        for instruction in self.instructions:
            if instruction.innerInstructionIndex is None:
                if instruction.instructionIndex <= top_index:
                    raise ValueError("instruction execution order is invalid")
                top_index = instruction.instructionIndex
                inner_index = -1
            else:
                if (
                    instruction.instructionIndex != top_index
                    or instruction.innerInstructionIndex <= inner_index
                ):
                    raise ValueError("inner instruction execution order is invalid")
                inner_index = instruction.innerInstructionIndex
        return self


class _Context(DomainModel):
    slot: NonNegativeInt


class _Result(DomainModel):
    context: _Context
    value: _Value

    @model_validator(mode="after")
    def validate_slot(self) -> _Result:
        if self.context.slot != self.value.transaction.slot:
            raise ValueError("notification and transaction slots disagree")
        return self


class _Params(DomainModel):
    subscription: NonNegativeInt
    result: _Result


class _Notification(DomainModel):
    jsonrpc: Literal["2.0"]
    method: Literal["parsedTransactionNotification"]
    params: _Params


@dataclass(frozen=True)
class _Program:
    name: ProgramName
    description: ProgramDescription
    instructions: dict[str, dict[str, Any]]


class _UnknownFields(ValueError):
    pass


class _UnsupportedType(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    del value
    raise ValueError("invalid JSON number")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("invalid JSON number")
    return result


class ParsedStreamsDecoder:
    """Decode pinned instruction fields only after raw journal persistence."""

    _ERROR_MESSAGES = {
        "unsupported_transport": "transport has no Parsed Streams decoder",
        "invalid_payload": "stored payload is not an unambiguous JSON object",
        "malformed_notification": "notification does not match the Parsed Streams contract",
        "unknown_fields": "notification contains fields outside the pinned decoding contract",
        "failed_transaction": "failed transaction is retained without decoded output",
        "unknown_instruction": "monitored instruction is not supported by both catalogs",
        "catalog_name_mismatch": "monitored instruction identity disagrees with discovery",
        "unsupported_argument_type": "instruction has an unsupported pinned argument type",
        "no_supported_instruction": "notification has no supported monitored instruction",
    }

    def __init__(self, settings: Settings, descriptions: Mapping[str, ProgramDescription]) -> None:
        programs: dict[str, _Program] = {}
        for snapshot in load_pinned_catalog(settings):
            description = descriptions.get(snapshot.program_id)
            if description is None or description.id != snapshot.program_id:
                raise DecoderConfigurationError("Parsed Streams discovery is incomplete")
            payload: Any = json.loads(snapshot.payload_json)
            instructions = {item["name"]: item for item in payload["instructions"]}
            programs[snapshot.program_id] = _Program(
                name=ProgramName(snapshot.program_name),
                description=description.model_copy(deep=True),
                instructions=instructions,
            )
        self._programs = programs

    def _outcome(self, event: DecoderInput, status: DecodeStatus, code: str) -> DecodeResult:
        return DecodeResult(
            raw_event_id=event.raw_event_id,
            status=status,
            safe_error_code=code,
            safe_error_message=self._ERROR_MESSAGES[code],
        )

    @staticmethod
    def _validate_arg(value: JsonValue, arg_type: Any) -> None:
        if arg_type == "bool":
            if type(value) is not bool:
                raise ValueError("argument must be a boolean")
        elif arg_type == "string":
            if not isinstance(value, str):
                raise ValueError("argument must be text")
        elif arg_type == "pubkey":
            if not isinstance(value, str):
                raise ValueError("argument must be a public key")
            _validate_base58_address(value)
        elif isinstance(arg_type, str) and re.fullmatch(r"[ui](8|16|32|64|128|256)", arg_type):
            signed = arg_type.startswith("i")
            if isinstance(value, str):
                pattern = r"-?(0|[1-9][0-9]*)" if signed else r"(0|[1-9][0-9]*)"
                if re.fullmatch(pattern, value) is None or value == "-0":
                    raise ValueError("argument must be an exact decimal integer")
                number = int(value)
            elif type(value) is int:
                number = value
            else:
                raise ValueError("argument must be an exact integer")
            bits = int(arg_type[1:])
            lower = -(2 ** (bits - 1)) if signed else 0
            upper = 2 ** (bits - int(signed)) - 1
            if not lower <= number <= upper:
                raise ValueError("argument is outside its pinned integer range")
        else:
            # Complex representations are not documented by the provider. Keep
            # them raw until their wire encoding can be verified explicitly.
            raise _UnsupportedType

    @classmethod
    def _validate_fields(cls, instruction: _Instruction, definition: dict[str, Any]) -> None:
        decoded = instruction.decoded
        if decoded is None:
            raise _UnknownFields
        expected_args = {item["name"]: item["type"] for item in definition["args"]}
        if set(decoded.args) - set(expected_args):
            raise _UnknownFields
        if set(decoded.args) != set(expected_args):
            raise ValueError("required instruction argument is missing")
        expected_accounts = [item["name"] for item in definition["accounts"]]
        account_names = [account.name for account in decoded.accounts]
        if set(account_names) - set(expected_accounts):
            raise _UnknownFields
        if account_names != expected_accounts:
            raise ValueError("instruction accounts are missing, duplicated or out of order")
        for name, arg_type in expected_args.items():
            cls._validate_arg(decoded.args[name], arg_type)

    def decode(self, event: DecoderInput) -> DecodeResult:
        if event.transport != "parsed_streams":
            return self._outcome(event, DecodeStatus.RAW_ONLY, "unsupported_transport")
        try:
            payload: Any = json.loads(
                event.payload_json,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
                parse_float=_finite_float,
            )
        except (TypeError, ValueError, RecursionError):
            return self._outcome(event, DecodeStatus.FAILED, "invalid_payload")
        if not isinstance(payload, dict):
            return self._outcome(event, DecodeStatus.FAILED, "invalid_payload")
        try:
            notification = _Notification.model_validate(payload)
        except ValidationError as error:
            unknown_only = all(item["type"] == "extra_forbidden" for item in error.errors())
            return self._outcome(
                event,
                DecodeStatus.RAW_ONLY if unknown_only else DecodeStatus.FAILED,
                "unknown_fields" if unknown_only else "malformed_notification",
            )
        value = notification.params.result.value
        if value.transaction.status == "error":
            return self._outcome(event, DecodeStatus.RAW_ONLY, "failed_transaction")
        typed: list[DecodedInstruction] = []
        for position, instruction in enumerate(value.instructions):
            program = self._programs.get(instruction.programId)
            if program is None:
                continue
            name = instruction.instructionName
            if instruction.programName != program.description.name:
                return self._outcome(event, DecodeStatus.RAW_ONLY, "catalog_name_mismatch")
            if (
                name is None
                or name not in program.instructions
                or name not in program.description.instructions
                or instruction.decoded is None
            ):
                return self._outcome(event, DecodeStatus.RAW_ONLY, "unknown_instruction")
            try:
                self._validate_fields(instruction, program.instructions[name])
            except _UnknownFields:
                return self._outcome(event, DecodeStatus.RAW_ONLY, "unknown_fields")
            except _UnsupportedType:
                return self._outcome(event, DecodeStatus.RAW_ONLY, "unsupported_argument_type")
            except ValueError:
                return self._outcome(event, DecodeStatus.FAILED, "malformed_notification")
            if any(
                account.name not in program.description.roles
                for account in instruction.decoded.accounts
            ):
                return self._outcome(event, DecodeStatus.RAW_ONLY, "catalog_name_mismatch")
            typed.append(
                DecodedInstruction(
                    program_id=instruction.programId,
                    program_name=program.name,
                    instruction_name=name,
                    instruction_index=instruction.instructionIndex,
                    is_inner=instruction.innerInstructionIndex is not None,
                    accounts=[
                        DecodedAccount(
                            name=account.name,
                            pubkey=account.pubkey,
                            is_signer=account.isSigner,
                            is_writable=account.isWritable,
                        )
                        for account in instruction.decoded.accounts
                    ],
                    args=instruction.decoded.args,
                    matched_filter=position in value.matchedIndexes,
                )
            )
        if not typed:
            return self._outcome(event, DecodeStatus.RAW_ONLY, "no_supported_instruction")
        return DecodeResult(
            raw_event_id=event.raw_event_id, status=DecodeStatus.DECODED, instructions=typed
        )
