"""Pinned Anchor event decoding from journaled JSON getTransaction results.

Runtime-attributed ``Program data:`` and verified Anchor self-CPI bytes are
events. Instruction names and ordinary text logs never create events. Event
indices count monitored emitted events across the transaction, while source
indices address compiled top-level instructions, including unlogged precompiles.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
from dataclasses import dataclass
from typing import Any

from app.config import BASE58_ALPHABET, Settings, _validate_base58_address
from app.db.catalog import load_pinned_catalog
from app.decoders import DecoderConfigurationError, DecodeResult, DecoderInput
from app.domain.enums import DecodeStatus
from app.domain.models import DecodedEvent, JsonObject, JsonValue, _signature

_MAX_PAYLOAD = 4 * 1024 * 1024
_MAX_LOGS = 8192
_MAX_EVENT_BYTES = 65536
_MAX_STRING_BYTES = 16384
_MAX_CONTAINER_ITEMS = 1024
_MAX_DECODED_ITEMS = 4096
_MAX_DEPTH = 16
_MAX_INSTRUCTIONS = 1024
_MAX_ACCOUNTS = 256
_EVENT_CPI_TAG = bytes.fromhex("e445a52e51cb9a1d")
_INVOKE = re.compile(r"Program ([1-9A-HJ-NP-Za-km-z]{32,44}) invoke \[([1-9][0-9]*)\]")
_EXIT = re.compile(r"Program ([1-9A-HJ-NP-Za-km-z]{32,44}) (success|failed: .*)")
# Agave processes these signature-verification precompiles without stable invoke
# logs. Other programs, including ComputeBudget, cannot be skipped.
_UNLOGGED_PRECOMPILES = frozenset(
    {
        "Ed25519SigVerify111111111111111111111111111",
        "KeccakSecp256k11111111111111111111111111111",
        "Secp256r1SigVerify1111111111111111111111111",
    }
)
_MONITORED_EVENTS = {
    "pump": frozenset(
        {
            "CreateEvent",
            "TradeEvent",
            "CompleteEvent",
            "PostCompleteBuyEvent",
            "CompletePumpAmmMigrationEvent",
        }
    ),
    "pump_amm": frozenset({"CreatePoolEvent", "BuyEvent", "SellEvent"}),
}


class _Rejected(ValueError):
    def __init__(self, code: str, status: DecodeStatus = DecodeStatus.FAILED) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError
    return number


def _reject_constant(value: str) -> Any:
    del value
    raise ValueError


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _Rejected("malformed_transaction")
    return value


def _array(value: Any, limit: int) -> list[Any]:
    if not isinstance(value, list) or len(value) > limit:
        raise _Rejected("malformed_transaction")
    return value


def _index(value: Any, size: int) -> int:
    if type(value) is not int or not 0 <= value < size:
        raise _Rejected("malformed_transaction")
    return value


def _address(value: Any) -> str:
    if not isinstance(value, str):
        raise _Rejected("malformed_transaction")
    try:
        return _validate_base58_address(value)
    except ValueError:
        raise _Rejected("malformed_transaction") from None


def _known_context(value: dict[str, Any], keys: set[str]) -> None:
    if set(value) - keys:
        raise _Rejected("unknown_context_fields", DecodeStatus.RAW_ONLY)


def _base58(data: bytes) -> str:
    number = int.from_bytes(data, "big")
    encoded = ""
    while number:
        number, digit = divmod(number, 58)
        encoded = BASE58_ALPHABET[digit] + encoded
    return "1" * (len(data) - len(data.lstrip(b"\x00"))) + encoded


def _instruction_bytes(encoded: str) -> bytes:
    # Decode in chunks to bound the cost of repeated large-integer operations.
    if len(encoded) > (_MAX_EVENT_BYTES + 8) * 2:
        raise _Rejected("event_resource_limit", DecodeStatus.RAW_ONLY)
    number = 0
    for start in range(0, len(encoded), 8):
        chunk = encoded[start : start + 8]
        digit = 0
        for character in chunk:
            digit = digit * 58 + BASE58_ALPHABET.index(character)
        number = number * 58 ** len(chunk) + digit
    leading_zeros = len(encoded) - len(encoded.lstrip("1"))
    size = (number.bit_length() + 7) // 8
    if size + leading_zeros > _MAX_EVENT_BYTES + 8:
        raise _Rejected("event_resource_limit", DecodeStatus.RAW_ONLY)
    return b"\0" * leading_zeros + number.to_bytes(size, "big")


@dataclass(frozen=True)
class _Program:
    events: dict[bytes, str]
    supported: frozenset[str]
    types: dict[str, dict[str, Any]]


class _BorshReader:
    """Bounded Borsh reader; unsupported IDL representations stay raw-only."""

    def __init__(self, data: bytes, types: dict[str, dict[str, Any]]) -> None:
        self.data = data
        self.types = types
        self.offset = 0
        self.remaining_items = _MAX_DECODED_ITEMS

    def read(self, size: int) -> bytes:
        if size < 0 or self.offset + size > len(self.data):
            raise _Rejected("malformed_event_data")
        result = self.data[self.offset : self.offset + size]
        self.offset += size
        return result

    def tag(self) -> bool:
        value = self.read(1)[0]
        if value not in (0, 1):
            raise _Rejected("malformed_event_data")
        return bool(value)

    def decode(self, field_type: Any, depth: int = 0) -> JsonValue:
        self.remaining_items -= 1
        if depth > _MAX_DEPTH or self.remaining_items < 0:
            raise _Rejected("event_resource_limit", DecodeStatus.RAW_ONLY)
        if field_type == "bool":
            return self.tag()
        if field_type == "pubkey":
            return _base58(self.read(32))
        if field_type == "string":
            size = int.from_bytes(self.read(4), "little")
            if size > _MAX_STRING_BYTES:
                raise _Rejected("event_resource_limit", DecodeStatus.RAW_ONLY)
            try:
                return self.read(size).decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                raise _Rejected("malformed_event_data") from None
        if isinstance(field_type, str) and re.fullmatch(r"[ui](8|16|32|64|128)", field_type):
            bits = int(field_type[1:])
            number = int.from_bytes(self.read(bits // 8), "little", signed=field_type[0] == "i")
            # Keep every wide amount exact. The pinned signed i64 fields are
            # timestamps; small numeric fields keep their native integer type.
            return str(number) if bits == 128 or field_type == "u64" else number
        if not isinstance(field_type, dict) or len(field_type) != 1:
            raise _Rejected("unsupported_event_layout", DecodeStatus.RAW_ONLY)
        if "defined" in field_type:
            definition = field_type["defined"]
            if not isinstance(definition, dict) or set(definition) != {"name"}:
                raise _Rejected("unsupported_event_layout", DecodeStatus.RAW_ONLY)
            name = definition["name"]
            if not isinstance(name, str) or name not in self.types:
                raise _Rejected("unsupported_event_layout", DecodeStatus.RAW_ONLY)
            return self.struct(self.types[name], depth + 1)
        if "option" in field_type:
            return self.decode(field_type["option"], depth + 1) if self.tag() else None
        if "vec" in field_type:
            size = int.from_bytes(self.read(4), "little")
            element = field_type["vec"]
        elif "array" in field_type:
            array = field_type["array"]
            if not isinstance(array, list) or len(array) != 2 or type(array[1]) is not int:
                raise _Rejected("unsupported_event_layout", DecodeStatus.RAW_ONLY)
            element, size = array
        else:
            raise _Rejected("unsupported_event_layout", DecodeStatus.RAW_ONLY)
        if not 0 <= size <= _MAX_CONTAINER_ITEMS or size > self.remaining_items:
            raise _Rejected("event_resource_limit", DecodeStatus.RAW_ONLY)
        return [self.decode(element, depth + 1) for _ in range(size)]

    def struct(self, definition: dict[str, Any], depth: int = 0) -> JsonObject:
        if depth > _MAX_DEPTH or definition.get("kind") != "struct":
            raise _Rejected("unsupported_event_layout", DecodeStatus.RAW_ONLY)
        fields = definition.get("fields")
        if not isinstance(fields, list) or len(fields) > _MAX_CONTAINER_ITEMS:
            raise _Rejected("unsupported_event_layout", DecodeStatus.RAW_ONLY)
        result: JsonObject = {}
        for field in fields:
            if (
                not isinstance(field, dict)
                or not isinstance(field.get("name"), str)
                or field["name"] in result
                or "type" not in field
            ):
                raise _Rejected("unsupported_event_layout", DecodeStatus.RAW_ONLY)
            result[field["name"]] = self.decode(field["type"], depth + 1)
        return result


@dataclass(frozen=True)
class _Instruction:
    program_id: str
    stack_height: int | None
    data: str
    accounts: tuple[str, ...]


@dataclass(frozen=True)
class _Frame:
    program_id: str
    event_cpi: bool = False


@dataclass(frozen=True)
class _Context:
    programs: list[_Instruction]
    inner: dict[int, list[_Instruction]] | None
    logs: list[str]
    failed: bool


def _instruction(value: Any, keys: list[str], *, inner: bool) -> _Instruction:
    instruction = _object(value)
    _known_context(instruction, {"programIdIndex", "accounts", "data", "stackHeight"})
    program_id = keys[_index(instruction.get("programIdIndex"), len(keys))]
    accounts = tuple(
        keys[_index(account, len(keys))]
        for account in _array(instruction.get("accounts"), _MAX_ACCOUNTS)
    )
    data = instruction.get("data")
    if (
        not isinstance(data, str)
        or len(data) > _MAX_EVENT_BYTES * 2
        or any(character not in BASE58_ALPHABET for character in data)
    ):
        raise _Rejected("malformed_transaction")
    height = instruction.get("stackHeight")
    if height is not None and (
        type(height) is not int or not (2 if inner else 1) <= height <= _MAX_DEPTH
    ):
        raise _Rejected("malformed_transaction")
    if not inner and height not in (None, 1):
        raise _Rejected("malformed_transaction")
    return _Instruction(program_id, height, data, accounts)


def _context(payload: dict[str, Any]) -> _Context:
    _known_context(payload, {"slot", "transaction", "meta", "blockTime", "version"})
    slot = payload.get("slot")
    if type(slot) is not int or not 0 <= slot < 2**64:
        raise _Rejected("malformed_transaction")
    version = payload.get("version", "legacy")
    if version != "legacy" and (type(version) is not int or version not in (0, 1)):
        raise _Rejected("unsupported_transaction_version", DecodeStatus.RAW_ONLY)
    transaction = _object(payload.get("transaction"))
    _known_context(transaction, {"signatures", "message"})
    signatures = _array(transaction.get("signatures"), _MAX_ACCOUNTS)
    if not signatures:
        raise _Rejected("malformed_transaction")
    for signature in signatures:
        if not isinstance(signature, str):
            raise _Rejected("malformed_transaction")
        try:
            _signature(signature)
        except ValueError:
            raise _Rejected("malformed_transaction") from None
    message = _object(transaction.get("message"))
    _known_context(
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
    if version == 1:
        # V1 resource limits are inline message fields, and ALTs are absent.
        config = _object(message.get("transactionConfig"))
        config_widths = {
            "computeUnitLimit": 32,
            "heapSize": 32,
            "loadedAccountsDataSizeLimit": 32,
            "priorityFee": 64,
        }
        _known_context(config, set(config_widths))
        if set(config) != set(config_widths) or "addressTableLookups" in message:
            raise _Rejected("malformed_transaction")
        for name, width in config_widths.items():
            value = config[name]
            if value is not None and (type(value) is not int or not 0 <= value < 2**width):
                raise _Rejected("malformed_transaction")
    elif "transactionConfig" in message:
        raise _Rejected("malformed_transaction")
    keys = [_address(key) for key in _array(message.get("accountKeys"), _MAX_ACCOUNTS)]
    if "header" in message:
        header = _object(message["header"])
        _known_context(
            header,
            {"numRequiredSignatures", "numReadonlySignedAccounts", "numReadonlyUnsignedAccounts"},
        )
        required = header.get("numRequiredSignatures")
        if (
            type(required) is not int
            or required != len(signatures)
            or not 1 <= required <= len(keys)
        ):
            raise _Rejected("malformed_transaction")
        for name, maximum in (
            ("numReadonlySignedAccounts", required),
            ("numReadonlyUnsignedAccounts", len(keys) - required),
        ):
            value = header.get(name)
            if type(value) is not int or not 0 <= value <= maximum:
                raise _Rejected("malformed_transaction")
    if "recentBlockhash" in message:
        _address(message["recentBlockhash"])
    block_time = payload.get("blockTime")
    if block_time is not None and (
        type(block_time) is not int or not -(2**63) <= block_time < 2**63
    ):
        raise _Rejected("malformed_transaction")
    meta = _object(payload.get("meta"))
    if "err" not in meta:
        raise _Rejected("malformed_transaction")
    loaded = meta.get("loadedAddresses")
    if loaded is not None:
        addresses = _object(loaded)
        _known_context(addresses, {"writable", "readonly"})
        writable = [_address(key) for key in _array(addresses.get("writable"), _MAX_ACCOUNTS)]
        readonly = [_address(key) for key in _array(addresses.get("readonly"), _MAX_ACCOUNTS)]
        if version != 0 and (writable or readonly):
            raise _Rejected("malformed_transaction")
        keys.extend(writable + readonly)
    lookups = message.get("addressTableLookups", [])
    expected = [0, 0]
    for item in _array(lookups, _MAX_ACCOUNTS):
        lookup = _object(item)
        _known_context(lookup, {"accountKey", "writableIndexes", "readonlyIndexes"})
        _address(lookup.get("accountKey"))
        for position, name in enumerate(("writableIndexes", "readonlyIndexes")):
            indices = _array(lookup.get(name), _MAX_ACCOUNTS)
            for index in indices:
                _index(index, _MAX_ACCOUNTS)
            expected[position] += len(indices)
    if lookups and (
        version == "legacy"
        or loaded is None
        or expected != [len(loaded["writable"]), len(loaded["readonly"])]
    ):
        raise _Rejected("malformed_transaction")
    if loaded is not None and not lookups and (loaded["writable"] or loaded["readonly"]):
        raise _Rejected("malformed_transaction")
    if not keys or len(keys) > _MAX_ACCOUNTS:
        raise _Rejected("malformed_transaction")
    instructions = [
        _instruction(item, keys, inner=False)
        for item in _array(message.get("instructions"), _MAX_INSTRUCTIONS)
    ]
    logs = _array(meta.get("logMessages"), _MAX_LOGS)
    if any(not isinstance(log, str) or len(log) > _MAX_EVENT_BYTES * 2 for log in logs):
        raise _Rejected("malformed_transaction")
    inner_context = meta.get("innerInstructions")
    inner: dict[int, list[_Instruction]] | None = None
    if inner_context is not None:
        inner = {}
        previous = -1
        count = 0
        for item in _array(inner_context, _MAX_INSTRUCTIONS):
            group = _object(item)
            _known_context(group, {"index", "instructions"})
            index = _index(group.get("index"), len(instructions))
            if index <= previous:
                raise _Rejected("malformed_transaction")
            previous = index
            compiled = _array(group.get("instructions"), _MAX_INSTRUCTIONS)
            count += len(compiled)
            if count > _MAX_INSTRUCTIONS:
                raise _Rejected("malformed_transaction")
            inner[index] = [_instruction(item, keys, inner=True) for item in compiled]
    return _Context(instructions, inner, logs, meta["err"] is not None)


class RawLogsDecoder:
    """Decode monitored binary events without classifying or normalizing them."""

    _ERROR_MESSAGES = {
        "unsupported_transport": "transport has no raw transaction decoder",
        "invalid_payload": "stored payload is not an unambiguous bounded JSON object",
        "malformed_transaction": "transaction context does not match the decoding contract",
        "unknown_context_fields": "transaction has unsupported fields in its execution context",
        "unsupported_transaction_version": "transaction version is outside the decoding contract",
        "failed_transaction": "failed transaction is retained without decoded output",
        "malformed_log_stack": "runtime log execution provenance is inconsistent",
        "incomplete_logs": "runtime logs do not cover the compiled execution context",
        "cpi_context_unavailable": "inner execution cannot be verified against compiled context",
        "truncated_logs": "truncated runtime logs are retained without decoded output",
        "failed_execution": "failed program execution is retained without decoded output",
        "unknown_event": "monitored binary event is outside the pinned event contract",
        "unsupported_event_layout": "binary event has an unsupported pinned layout",
        "malformed_event_data": "monitored binary event data is malformed or incomplete",
        "event_resource_limit": "binary event exceeds bounded decoding resources",
        "ambiguous_event_provenance": "event CPI is not a verified monitored self-invocation",
        "ambiguous_event_representation": "event CPI frame has conflicting event representations",
        "no_supported_event": "transaction has no supported monitored emitted event",
    }

    def __init__(self, settings: Settings, *, accepted_transport: str = "logs_subscribe") -> None:
        # RPC reconciliation is explicit for the already selected stream transport.
        # This never opens a second socket or selects a fallback transport.
        if accepted_transport not in {"logs_subscribe", "parsed_streams"}:
            raise DecoderConfigurationError("raw transaction transport is unsupported")
        self._accepted_transport = accepted_transport
        self._programs: dict[str, _Program] = {}
        for snapshot in load_pinned_catalog(settings):
            payload: Any = json.loads(snapshot.payload_json)
            try:
                supported = _MONITORED_EVENTS[snapshot.program_name]
                types = {item["name"]: item["type"] for item in payload["types"]}
                events: dict[bytes, str] = {}
                for event in payload["events"]:
                    discriminator = event["discriminator"]
                    if (
                        not isinstance(discriminator, list)
                        or len(discriminator) != 8
                        or any(
                            type(byte) is not int or not 0 <= byte <= 255 for byte in discriminator
                        )
                        or bytes(discriminator) in events
                    ):
                        raise ValueError
                    events[bytes(discriminator)] = event["name"]
                if not supported <= set(events.values()) or not supported <= set(types):
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                raise DecoderConfigurationError("pinned raw event catalog is incomplete") from None
            self._programs[snapshot.program_id] = _Program(events, supported, types)

    def _outcome(self, event: DecoderInput, rejection: _Rejected) -> DecodeResult:
        return DecodeResult(
            raw_event_id=event.raw_event_id,
            status=rejection.status,
            safe_error_code=rejection.code,
            safe_error_message=self._ERROR_MESSAGES[rejection.code],
        )

    def _binary_event(self, encoded: str, program: _Program) -> tuple[str, JsonObject]:
        if len(encoded) > ((_MAX_EVENT_BYTES + 2) // 3) * 4:
            raise _Rejected("event_resource_limit", DecodeStatus.RAW_ONLY)
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise _Rejected("malformed_event_data") from None
        # Reject alternate padding/noncanonical encodings and multiple slices;
        # Anchor emit! uses exactly one canonical Base64 event buffer.
        if base64.b64encode(data).decode("ascii") != encoded:
            raise _Rejected("malformed_event_data")
        return self._event_data(data, program)

    @staticmethod
    def _event_data(data: bytes, program: _Program) -> tuple[str, JsonObject]:
        if len(data) > _MAX_EVENT_BYTES:
            raise _Rejected("event_resource_limit", DecodeStatus.RAW_ONLY)
        if len(data) < 8:
            raise _Rejected("malformed_event_data")
        name = program.events.get(data[:8])
        if name is None or name not in program.supported:
            raise _Rejected("unknown_event", DecodeStatus.RAW_ONLY)
        reader = _BorshReader(data[8:], program.types)
        fields = reader.struct(program.types[name])
        if reader.offset != len(reader.data):
            raise _Rejected("unsupported_event_layout", DecodeStatus.RAW_ONLY)
        return name, fields

    def _events(self, context: _Context) -> list[DecodedEvent]:
        stack: list[_Frame] = []
        top_index = -1
        next_top = 0
        inner_position = 0
        events: list[DecodedEvent] = []

        def append_event(program_id: str, name: str, fields: JsonObject) -> None:
            events.append(
                DecodedEvent(
                    event_name=name,
                    program_id=program_id,
                    event_index=len(events),
                    source_instruction_index=top_index,
                    fields=fields,
                    decode_status=DecodeStatus.DECODED,
                )
            )

        def finish_inner() -> None:
            if context.inner is not None and inner_position != len(
                context.inner.get(top_index, [])
            ):
                raise _Rejected("incomplete_logs", DecodeStatus.RAW_ONLY)

        for log in context.logs:
            if log == "Log truncated" or log.startswith("Log truncated "):
                raise _Rejected("truncated_logs", DecodeStatus.RAW_ONLY)
            invocation = _INVOKE.fullmatch(log)
            exit_log = _EXIT.fullmatch(log)
            if invocation:
                program_id = invocation[1]
                depth_text = invocation[2]
                if len(depth_text) > 2:
                    raise _Rejected("malformed_log_stack")
                depth = int(depth_text)
                if depth != len(stack) + 1 or depth > _MAX_DEPTH:
                    raise _Rejected("malformed_log_stack")
                if stack and stack[-1].event_cpi:
                    raise _Rejected("ambiguous_event_representation", DecodeStatus.RAW_ONLY)
                _address(program_id)
                if depth == 1:
                    finish_inner()
                    while (
                        next_top < len(context.programs)
                        and context.programs[next_top].program_id in _UNLOGGED_PRECOMPILES
                        and context.programs[next_top].program_id != program_id
                    ):
                        if context.inner and context.inner.get(next_top):
                            raise _Rejected("malformed_log_stack")
                        next_top += 1
                    if (
                        next_top >= len(context.programs)
                        or context.programs[next_top].program_id != program_id
                    ):
                        raise _Rejected("incomplete_logs", DecodeStatus.RAW_ONLY)
                    top_index = next_top
                    instruction = context.programs[next_top]
                    next_top += 1
                    inner_position = 0
                else:
                    if context.inner is None:
                        raise _Rejected("cpi_context_unavailable", DecodeStatus.RAW_ONLY)
                    compiled = context.inner.get(top_index, [])
                    if inner_position >= len(compiled):
                        raise _Rejected("malformed_log_stack")
                    instruction = compiled[inner_position]
                    if instruction.program_id != program_id or instruction.stack_height not in (
                        None,
                        depth,
                    ):
                        raise _Rejected("malformed_log_stack")
                    inner_position += 1
                program = self._programs.get(program_id)
                is_event_cpi = False
                if program is not None:
                    raw = _instruction_bytes(instruction.data)
                    if raw.startswith(_EVENT_CPI_TAG):
                        if (
                            not stack
                            or stack[-1].program_id != program_id
                            or len(instruction.accounts) != 1
                        ):
                            raise _Rejected("ambiguous_event_provenance", DecodeStatus.RAW_ONLY)
                        name, fields = self._event_data(raw[8:], program)
                        append_event(program_id, name, fields)
                        is_event_cpi = True
                stack.append(_Frame(program_id, is_event_cpi))
            elif exit_log:
                if not stack or stack[-1].program_id != exit_log[1]:
                    raise _Rejected("malformed_log_stack")
                if exit_log[2] != "success":
                    raise _Rejected("failed_execution", DecodeStatus.RAW_ONLY)
                stack.pop()
            elif log.startswith("Program data:"):
                if not stack:
                    raise _Rejected("malformed_log_stack")
                if stack[-1].event_cpi:
                    raise _Rejected("ambiguous_event_representation", DecodeStatus.RAW_ONLY)
                program = self._programs.get(stack[-1].program_id)
                if program is not None:
                    if not log.startswith("Program data: "):
                        raise _Rejected("malformed_event_data")
                    name, fields = self._binary_event(log[len("Program data: ") :], program)
                    append_event(stack[-1].program_id, name, fields)
            elif log.startswith("Program ") and (
                " invoke [" in log or " success" in log or " failed:" in log
            ):
                # Program-generated text has its own 'Program log:' prefix and
                # cannot substitute for malformed runtime control messages.
                if not log.startswith("Program log:"):
                    raise _Rejected("malformed_log_stack")
        if stack:
            raise _Rejected("incomplete_logs", DecodeStatus.RAW_ONLY)
        finish_inner()
        while (
            next_top < len(context.programs)
            and context.programs[next_top].program_id in _UNLOGGED_PRECOMPILES
        ):
            if context.inner and context.inner.get(next_top):
                raise _Rejected("malformed_log_stack")
            next_top += 1
        if next_top != len(context.programs):
            raise _Rejected("incomplete_logs", DecodeStatus.RAW_ONLY)
        return events

    def decode(self, event: DecoderInput) -> DecodeResult:
        if event.transport != self._accepted_transport:
            return self._outcome(event, _Rejected("unsupported_transport", DecodeStatus.RAW_ONLY))
        if len(event.payload_json) > _MAX_PAYLOAD:
            return self._outcome(event, _Rejected("invalid_payload"))
        try:
            payload: Any = json.loads(
                event.payload_json,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
                parse_float=_finite_float,
            )
            if not isinstance(payload, dict):
                raise ValueError
        except (TypeError, ValueError, RecursionError):
            return self._outcome(event, _Rejected("invalid_payload"))
        try:
            context = _context(payload)
            if context.failed:
                raise _Rejected("failed_transaction", DecodeStatus.RAW_ONLY)
            events = self._events(context)
            if not events:
                raise _Rejected("no_supported_event", DecodeStatus.RAW_ONLY)
        except _Rejected as rejection:
            return self._outcome(event, rejection)
        return DecodeResult(
            raw_event_id=event.raw_event_id, status=DecodeStatus.DECODED, events=events
        )
