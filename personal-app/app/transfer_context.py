"""Bounded compiled transaction context for read-only transfer extraction.

Balance changes and instruction bytes remain separate evidence. Inner instruction
execution is verified only through complete, correlated runtime control logs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from app.config import BASE58_ALPHABET, _validate_base58_address
from app.domain.models import _signature

ExecutionStatus = Literal["succeeded", "failed", "unverified"]
_MAX_KEYS = 256
_MAX_INSTRUCTIONS = 1024
_MAX_LOGS = 8192
_MAX_DEPTH = 16
_MAX_INSTRUCTION_BYTES = 4096
_MAX_INSTRUCTION_CHARS = 5594
_MAX_LOG_CHARS = 131072
_INVOKE = re.compile(r"Program ([1-9A-HJ-NP-Za-km-z]{32,44}) invoke \[([1-9][0-9]*)\]")
_EXIT = re.compile(r"Program ([1-9A-HJ-NP-Za-km-z]{32,44}) (success|failed: .*)")
_CONTROL = re.compile(r"\b(?:invoke|success|failed)\b")
_UNLOGGED_PRECOMPILES = frozenset(
    {
        "Ed25519SigVerify111111111111111111111111111",
        "KeccakSecp256k11111111111111111111111111111",
        "Secp256r1SigVerify1111111111111111111111111",
    }
)


class TransferContextError(ValueError):
    """Fixed diagnostics never interpolate provider-controlled content."""

    _MESSAGES = {
        "invalid_transfer_context": "compiled transfer context is malformed",
        "unsupported_transfer_context": "compiled transfer context has unsupported fields",
        "unsupported_transaction_version": "transaction version is unsupported",
        "transfer_context_resource_limit": "compiled transfer context exceeds bounded resources",
        "account_resolution_unavailable": "transaction lookup addresses are unavailable",
    }

    def __init__(self, code: str) -> None:
        self.code = code if code in self._MESSAGES else "invalid_transfer_context"
        super().__init__(self._MESSAGES[self.code])


@dataclass(frozen=True)
class TransferInstructionContext:
    program_id: str
    accounts: tuple[str, ...]
    account_indices: tuple[int, ...]
    data: str
    top_level_index: int
    inner_instruction_index: int | None
    stack_height: int | None
    execution_status: ExecutionStatus
    ancestor_programs: tuple[str, ...] = ()


@dataclass(frozen=True)
class TransferContext:
    account_keys: tuple[str, ...]
    fee_payer: str
    signers: tuple[str, ...] | None
    instructions: tuple[TransferInstructionContext, ...]
    warnings: tuple[str, ...]
    transaction_failed: bool


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TransferContextError("invalid_transfer_context")
    return value


def _known(value: dict[str, Any], names: set[str]) -> None:
    if set(value) - names:
        raise TransferContextError("unsupported_transfer_context")


def _array(value: Any, limit: int) -> list[Any]:
    if not isinstance(value, list):
        raise TransferContextError("invalid_transfer_context")
    if len(value) > limit:
        raise TransferContextError("transfer_context_resource_limit")
    return value


def _address(value: Any) -> str:
    if not isinstance(value, str):
        raise TransferContextError("invalid_transfer_context")
    try:
        return _validate_base58_address(value)
    except ValueError:
        raise TransferContextError("invalid_transfer_context") from None


def _index(value: Any, size: int) -> int:
    if type(value) is not int or not 0 <= value < size:
        raise TransferContextError("invalid_transfer_context")
    return value


def _data(value: Any) -> str:
    if not isinstance(value, str):
        raise TransferContextError("invalid_transfer_context")
    if len(value) > _MAX_INSTRUCTION_CHARS:
        raise TransferContextError("transfer_context_resource_limit")
    if any(character not in BASE58_ALPHABET for character in value):
        raise TransferContextError("invalid_transfer_context")
    number = 0
    for start in range(0, len(value), 8):
        chunk = value[start : start + 8]
        digit = 0
        for character in chunk:
            digit = digit * 58 + BASE58_ALPHABET.index(character)
        number = number * 58 ** len(chunk) + digit
    size = len(value) - len(value.lstrip("1")) + (number.bit_length() + 7) // 8
    if size > _MAX_INSTRUCTION_BYTES:
        raise TransferContextError("transfer_context_resource_limit")
    return value


def _version_and_keys(
    payload: dict[str, Any], message: dict[str, Any], meta: dict[str, Any], warnings: list[str]
) -> tuple[str, ...]:
    version = payload.get("version")
    if version is None:
        warnings.append("transaction_version_unavailable")
    elif version != "legacy" and (type(version) is not int or version not in (0, 1)):
        raise TransferContextError("unsupported_transaction_version")
    config_present = "transactionConfig" in message
    if version == 1 and not config_present:
        raise TransferContextError("invalid_transfer_context")
    if config_present:
        if version in ("legacy", 0) or "addressTableLookups" in message:
            raise TransferContextError("invalid_transfer_context")
        config = _object(message["transactionConfig"])
        widths = {
            "computeUnitLimit": 32,
            "heapSize": 32,
            "loadedAccountsDataSizeLimit": 32,
            "priorityFee": 64,
        }
        _known(config, set(widths))
        if set(config) != set(widths):
            raise TransferContextError("invalid_transfer_context")
        for name, width in widths.items():
            value = config[name]
            if value is not None and (type(value) is not int or not 0 <= value < 2**width):
                raise TransferContextError("invalid_transfer_context")
    keys = [_address(key) for key in _array(message.get("accountKeys"), _MAX_KEYS)]
    if not keys:
        raise TransferContextError("invalid_transfer_context")
    lookups = _array(message.get("addressTableLookups", []), _MAX_KEYS)
    if version == 1 and "addressTableLookups" in message:
        raise TransferContextError("invalid_transfer_context")
    if version == "legacy" and lookups:
        raise TransferContextError("invalid_transfer_context")
    expected = [0, 0]
    for raw in lookups:
        lookup = _object(raw)
        _known(lookup, {"accountKey", "writableIndexes", "readonlyIndexes"})
        _address(lookup.get("accountKey"))
        for position, name in enumerate(("writableIndexes", "readonlyIndexes")):
            indices = _array(lookup.get(name), _MAX_KEYS)
            for index in indices:
                _index(index, _MAX_KEYS)
            expected[position] += len(indices)
    loaded = meta.get("loadedAddresses")
    writable: list[str] = []
    readonly: list[str] = []
    if loaded is not None:
        addresses = _object(loaded)
        _known(addresses, {"writable", "readonly"})
        writable = [_address(key) for key in _array(addresses.get("writable"), _MAX_KEYS)]
        readonly = [_address(key) for key in _array(addresses.get("readonly"), _MAX_KEYS)]
    if (writable or readonly) and (version in ("legacy", 1) or config_present):
        raise TransferContextError("invalid_transfer_context")
    if lookups and loaded is None:
        raise TransferContextError("account_resolution_unavailable")
    if expected != [len(writable), len(readonly)]:
        raise TransferContextError("invalid_transfer_context")
    keys.extend(writable + readonly)
    if len(keys) > _MAX_KEYS:
        raise TransferContextError("transfer_context_resource_limit")
    if len(set(keys)) != len(keys):
        raise TransferContextError("invalid_transfer_context")
    return tuple(keys)


def _signers(
    transaction: dict[str, Any], message: dict[str, Any], warnings: list[str]
) -> tuple[str, ...] | None:
    signatures = _array(transaction.get("signatures"), _MAX_KEYS)
    if not signatures:
        raise TransferContextError("invalid_transfer_context")
    for signature in signatures:
        if not isinstance(signature, str):
            raise TransferContextError("invalid_transfer_context")
        try:
            _signature(signature)
        except ValueError:
            raise TransferContextError("invalid_transfer_context") from None
    if "header" not in message:
        warnings.append("signer_context_unavailable")
        return None
    header = _object(message["header"])
    _known(
        header,
        {"numRequiredSignatures", "numReadonlySignedAccounts", "numReadonlyUnsignedAccounts"},
    )
    required = header.get("numRequiredSignatures")
    keys = message["accountKeys"]
    if type(required) is not int or not 1 <= required <= len(keys) or required != len(signatures):
        raise TransferContextError("invalid_transfer_context")
    for name, maximum in (
        ("numReadonlySignedAccounts", required - 1),
        ("numReadonlyUnsignedAccounts", len(keys) - required),
    ):
        count = header.get(name)
        if type(count) is not int or not 0 <= count <= maximum:
            raise TransferContextError("invalid_transfer_context")
    return tuple(keys[:required])


def _instruction(
    raw: Any,
    keys: tuple[str, ...],
    top: int,
    inner: int | None,
    failed: bool,
) -> TransferInstructionContext:
    item = _object(raw)
    _known(item, {"programIdIndex", "accounts", "data", "stackHeight"})
    program = keys[_index(item.get("programIdIndex"), len(keys))]
    indices = tuple(_index(index, len(keys)) for index in _array(item.get("accounts"), _MAX_KEYS))
    height = item.get("stackHeight")
    if height is not None and (
        type(height) is not int or not (1 if inner is None else 2) <= height <= _MAX_DEPTH
    ):
        raise TransferContextError("invalid_transfer_context")
    if inner is None and height not in (None, 1):
        raise TransferContextError("invalid_transfer_context")
    return TransferInstructionContext(
        program,
        tuple(keys[index] for index in indices),
        indices,
        _data(item.get("data")),
        top,
        inner,
        height,
        "failed" if failed else "succeeded" if inner is None else "unverified",
    )


@dataclass
class _Frame:
    index: int
    descendants: list[int] = field(default_factory=list)


class _UnverifiedLogs(Exception):
    pass


def _correlate(
    instructions: list[TransferInstructionContext],
    tops: list[int],
    inner: dict[int, list[int]],
    logs: list[str],
) -> list[TransferInstructionContext]:
    result = list(instructions)
    stack: list[_Frame] = []
    next_top = 0
    current_top = -1
    next_inner = 0

    def finish_inner() -> None:
        if next_inner != len(inner.get(current_top, [])):
            raise _UnverifiedLogs

    def skip_precompiles(program: str | None = None) -> None:
        nonlocal next_top
        while next_top < len(tops):
            instruction = result[tops[next_top]]
            if (
                instruction.program_id not in _UNLOGGED_PRECOMPILES
                or instruction.program_id == program
            ):
                break
            if inner.get(instruction.top_level_index):
                raise _UnverifiedLogs
            next_top += 1

    for log in logs:
        if log == "Log truncated" or log.startswith("Log truncated "):
            raise _UnverifiedLogs
        invocation = _INVOKE.fullmatch(log)
        exit_log = _EXIT.fullmatch(log)
        if invocation:
            program, height_text = invocation.groups()
            if len(height_text) > 2:
                raise _UnverifiedLogs
            height = int(height_text)
            if height != len(stack) + 1 or height > _MAX_DEPTH:
                raise _UnverifiedLogs
            _address(program)
            if height == 1:
                finish_inner()
                skip_precompiles(program)
                if next_top >= len(tops):
                    raise _UnverifiedLogs
                index = tops[next_top]
                if result[index].program_id != program:
                    raise _UnverifiedLogs
                current_top = result[index].top_level_index
                next_top += 1
                next_inner = 0
            else:
                compiled = inner.get(current_top, [])
                if next_inner >= len(compiled):
                    raise _UnverifiedLogs
                index = compiled[next_inner]
                if result[index].program_id != program or result[index].stack_height not in (
                    None,
                    height,
                ):
                    raise _UnverifiedLogs
                next_inner += 1
                result[index] = replace(
                    result[index],
                    ancestor_programs=tuple(result[frame.index].program_id for frame in stack),
                )
                for frame in stack:
                    frame.descendants.append(index)
            stack.append(_Frame(index))
        elif exit_log:
            program, outcome = exit_log.groups()
            if not stack or result[stack[-1].index].program_id != program:
                raise _UnverifiedLogs
            frame = stack.pop()
            if outcome != "success":
                # A successful transaction can contain a caught failed CPI. Its
                # successful child logs do not establish a committed transfer.
                for index in [frame.index, *frame.descendants]:
                    if result[index].inner_instruction_index is not None:
                        result[index] = replace(result[index], execution_status="failed")
                if result[frame.index].inner_instruction_index is None:
                    raise _UnverifiedLogs
            elif result[frame.index].inner_instruction_index is not None:
                result[frame.index] = replace(result[frame.index], execution_status="succeeded")
        elif (
            log.startswith("Program ")
            and not log.startswith(("Program log:", "Program data:", "Program return:"))
            and _CONTROL.search(log[8:])
        ):
            raise _UnverifiedLogs
    if stack:
        raise _UnverifiedLogs
    finish_inner()
    skip_precompiles()
    if next_top != len(tops):
        raise _UnverifiedLogs
    return result


def parse_transfer_context(payload: dict[str, Any]) -> TransferContext:
    """Resolve instruction roles without interpreting bytes or balance deltas."""
    data = _object(payload)
    transaction = _object(data.get("transaction"))
    _known(transaction, {"signatures", "message"})
    message = _object(transaction.get("message"))
    _known(
        message,
        {
            "accountKeys",
            "instructions",
            "header",
            "recentBlockhash",
            "addressTableLookups",
            "transactionConfig",
        },
    )
    meta = _object(data.get("meta"))
    if "err" not in meta or (meta["err"] is not None and not isinstance(meta["err"], str | dict)):
        raise TransferContextError("invalid_transfer_context")
    failed = meta["err"] is not None
    warnings: list[str] = []
    keys = _version_and_keys(data, message, meta, warnings)
    signers = _signers(transaction, message, warnings)
    if "recentBlockhash" in message:
        _address(message["recentBlockhash"])
    raw_top = _array(message.get("instructions"), _MAX_INSTRUCTIONS)
    raw_inner = meta.get("innerInstructions")
    groups: dict[int, list[Any]] = {}
    if raw_inner is None:
        warnings.append("inner_instruction_context_unavailable")
    else:
        previous = -1
        count = len(raw_top)
        for raw in _array(raw_inner, _MAX_INSTRUCTIONS):
            group = _object(raw)
            _known(group, {"index", "instructions"})
            index = _index(group.get("index"), len(raw_top))
            if index <= previous:
                raise TransferContextError("invalid_transfer_context")
            previous = index
            items = _array(group.get("instructions"), _MAX_INSTRUCTIONS)
            count += len(items)
            if count > _MAX_INSTRUCTIONS:
                raise TransferContextError("transfer_context_resource_limit")
            groups[index] = items
    instructions: list[TransferInstructionContext] = []
    top_indices: list[int] = []
    inner_indices: dict[int, list[int]] = {}
    for top, raw in enumerate(raw_top):
        top_indices.append(len(instructions))
        instructions.append(_instruction(raw, keys, top, None, failed))
        inner_indices[top] = []
        for inner, raw in enumerate(groups.get(top, [])):
            inner_indices[top].append(len(instructions))
            instructions.append(_instruction(raw, keys, top, inner, failed))
    raw_logs = meta.get("logMessages")
    logs: list[str] | None = None
    if raw_logs is not None:
        values = _array(raw_logs, _MAX_LOGS)
        if any(not isinstance(log, str) for log in values):
            warnings.append("runtime_log_context_invalid")
        elif any(len(log) > _MAX_LOG_CHARS for log in values):
            raise TransferContextError("transfer_context_resource_limit")
        else:
            logs = values
    reported_cpi = any(
        (match := _INVOKE.fullmatch(log)) is not None and match[2] != "1" for log in logs or []
    )
    if (any(inner_indices.values()) or reported_cpi) and not failed:
        try:
            if not logs:
                raise _UnverifiedLogs
            instructions = _correlate(instructions, top_indices, inner_indices, logs)
        except (_UnverifiedLogs, TransferContextError):
            warnings.append("inner_execution_unverified")
    return TransferContext(keys, keys[0], signers, tuple(instructions), tuple(warnings), failed)
