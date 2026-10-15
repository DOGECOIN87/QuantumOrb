"""Validated wallet-history requests and immutable full-transaction observations."""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, StrictStr, ValidationError, field_validator, model_validator

from app.config import _validate_base58_address
from app.domain.models import DomainModel, JsonValue, NonNegativeInt, _signature

_U64_MAX = (1 << 64) - 1
_I64_MIN = -(1 << 63)
_I64_MAX = (1 << 63) - 1
_DECIMAL_AMOUNT = re.compile(r"0|[1-9][0-9]*")
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
HistoryLimit = Annotated[int, Field(strict=True, ge=1, le=1000)]


class HistoryValidationError(ValueError):
    """Only fixed diagnostics cross the untrusted history-response boundary."""

    _MESSAGES = {
        "invalid_history_request": "wallet history request is outside the supported bounds",
        "invalid_history_page": "wallet history page does not match the full response contract",
        "unknown_history_page_fields": "wallet history page has unsupported fields",
        "invalid_history_pagination": "wallet history pagination token is invalid",
        "invalid_history_transaction": "wallet history transaction context is malformed",
        "unknown_history_transaction_fields": "wallet history transaction has unsupported fields",
        "history_metadata_unavailable": "wallet history transaction metadata is unavailable",
        "invalid_history_amount": "wallet history raw amount is not an exact unsigned integer",
        "invalid_history_version": "wallet history transaction version is unsupported",
        "invalid_history_json": "wallet history payload is not finite JSON data",
    }

    def __init__(self, code: str) -> None:
        self.code = code if code in self._MESSAGES else "invalid_history_transaction"
        super().__init__(self._MESSAGES[self.code])


def _token(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 1024 or not value.isprintable():
        raise HistoryValidationError("invalid_history_pagination")
    return value


def _version(value: object) -> Literal["legacy", 0, 1] | None:
    if value is None:
        return None
    if value == "legacy":
        return "legacy"
    if type(value) is int and value in (0, 1):
        return 0 if value == 0 else 1
    raise HistoryValidationError("invalid_history_version")


def _transaction_error(value: object) -> JsonValue:
    if value is None or isinstance(value, str | dict):
        return value
    raise HistoryValidationError("history_metadata_unavailable")


class HistoryQuery(DomainModel):
    """One fixed, bounded history window; its opaque continuation keeps that scope."""

    model_config = ConfigDict(frozen=True)

    wallet: StrictStr
    start_time: datetime
    end_time: datetime
    sort_order: Literal["asc", "desc"] = "asc"
    status: Literal["succeeded", "failed", "any"] = "succeeded"
    token_accounts: Literal["none", "balanceChanged", "all"] = "balanceChanged"
    commitment: Literal["confirmed", "finalized"] = "finalized"
    page_size: HistoryLimit = 100
    max_transactions: HistoryLimit = 1000
    pagination_token: StrictStr | None = None

    _wallet_check = field_validator("wallet")(_validate_base58_address)
    _token_check = field_validator("pagination_token", mode="before")(_token)

    @field_validator("start_time", "end_time", mode="before")
    @classmethod
    def utc_seconds(cls, value: object) -> datetime:
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
            or value.microsecond != 0
        ):
            raise ValueError("history timestamps must be timezone-aware whole-second datetimes")
        normalized = value.astimezone(UTC)
        if normalized.microsecond != 0:
            raise ValueError("history timestamps must have whole-second UTC precision")
        if normalized.timestamp() < 0:
            raise ValueError("history timestamps must not precede the Unix epoch")
        return normalized

    @model_validator(mode="after")
    def bounded_window(self) -> HistoryQuery:
        if not timedelta(0) < self.end_time - self.start_time <= timedelta(days=30):
            raise ValueError("history window must be positive and no longer than thirty days")
        return self

    def rpc_params(self, limit: int, pagination_token: str | None) -> list[Any]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise HistoryValidationError("invalid_history_request")
        token = _token(pagination_token)
        options: dict[str, Any] = {
            "transactionDetails": "full",
            "encoding": "json",
            "maxSupportedTransactionVersion": 1,
            "sortOrder": self.sort_order,
            "commitment": self.commitment,
            "limit": limit,
            "filters": {
                "blockTime": {
                    "gte": int(self.start_time.timestamp()),
                    "lt": int(self.end_time.timestamp()),
                },
                "status": self.status,
                "tokenAccounts": self.token_accounts,
            },
        }
        if token is not None:
            options["paginationToken"] = token
        return [self.wallet, options]


class HistoryTransaction(DomainModel):
    """Full immutable JSON evidence, without transfer or instruction interpretation."""

    model_config = ConfigDict(frozen=True)

    raw_event_id: PositiveInt
    page_raw_event_id: PositiveInt
    signature: StrictStr
    slot: Annotated[int, Field(strict=True, ge=0, le=_U64_MAX)]
    transaction_index: NonNegativeInt
    block_time: Annotated[int, Field(strict=True, ge=_I64_MIN, le=_I64_MAX)] | None
    version: Literal["legacy", 0, 1] | None
    transaction_error: JsonValue | None
    payload_json: StrictStr

    _signature_check = field_validator("signature")(_signature)
    _version_check = field_validator("version", mode="before")(_version)
    _error_check = field_validator("transaction_error", mode="before")(_transaction_error)

    @property
    def status(self) -> Literal["succeeded", "failed"]:
        return "succeeded" if self.transaction_error is None else "failed"


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise HistoryValidationError("invalid_history_transaction")
    return value


def _u64(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _U64_MAX:
        raise HistoryValidationError("invalid_history_amount")
    return value


def _addresses(value: object) -> list[str]:
    if not isinstance(value, list):
        raise HistoryValidationError("invalid_history_transaction")
    result = []
    for address in value:
        if not isinstance(address, str):
            raise HistoryValidationError("invalid_history_transaction")
        try:
            result.append(_validate_base58_address(address))
        except ValueError:
            raise HistoryValidationError("invalid_history_transaction") from None
    return result


def _validate_json(value: object, depth: int = 0) -> None:
    if depth > 64:
        raise HistoryValidationError("invalid_history_json")
    if value is None or type(value) in {str, int, bool}:
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            _validate_json(item, depth + 1)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _validate_json(item, depth + 1)
        return
    raise HistoryValidationError("invalid_history_json")


def _validate_amounts(meta: dict[str, Any], message: dict[str, Any], keys: list[str]) -> None:
    if not {"fee", "preBalances", "postBalances"} <= set(meta):
        raise HistoryValidationError("history_metadata_unavailable")
    _u64(meta["fee"])
    balances = []
    for name in ("preBalances", "postBalances"):
        values = meta[name]
        if not isinstance(values, list):
            raise HistoryValidationError("invalid_history_amount")
        balances.append([_u64(value) for value in values])
    if len(balances[0]) != len(balances[1]) or len(balances[0]) < len(keys):
        raise HistoryValidationError("invalid_history_transaction")
    expected: int | None = len(keys)
    loaded = meta.get("loadedAddresses")
    if loaded is not None:
        loaded = _object(loaded)
        expected = (
            len(keys)
            + len(_addresses(loaded.get("writable")))
            + len(_addresses(loaded.get("readonly")))
        )
    elif message.get("addressTableLookups"):
        # Older metadata may omit resolution. T022 must preserve that uncertainty.
        expected = None
    if expected is not None and len(balances[0]) != expected:
        raise HistoryValidationError("invalid_history_transaction")
    for name in ("preTokenBalances", "postTokenBalances"):
        tokens = meta.get(name)
        if tokens is None:
            continue
        if not isinstance(tokens, list):
            raise HistoryValidationError("invalid_history_amount")
        for token in tokens:
            token = _object(token)
            if "uiTokenAmount" not in token:
                continue
            amount = _object(token["uiTokenAmount"])
            if "amount" in amount:
                raw = amount["amount"]
                if (
                    not isinstance(raw, str)
                    or _DECIMAL_AMOUNT.fullmatch(raw) is None
                    or len(raw) > 20
                    or int(raw) > _U64_MAX
                ):
                    raise HistoryValidationError("invalid_history_amount")


def parse_history_transaction(
    payload: object, *, raw_event_id: int, page_raw_event_id: int
) -> HistoryTransaction:
    """Validate a journaled full JSON record without deriving transaction facts."""
    data = _object(payload)
    allowed = {"slot", "transactionIndex", "blockTime", "transaction", "meta", "version"}
    if set(data) - allowed:
        raise HistoryValidationError("unknown_history_transaction_fields")
    if not {"slot", "transactionIndex", "blockTime", "transaction", "meta"} <= set(data):
        raise HistoryValidationError("invalid_history_transaction")
    _validate_json(data)
    transaction = _object(data["transaction"])
    signatures = transaction.get("signatures")
    if not isinstance(signatures, list) or not signatures:
        raise HistoryValidationError("invalid_history_transaction")
    try:
        for signature in signatures:
            if not isinstance(signature, str):
                raise ValueError
            _signature(signature)
    except ValueError:
        raise HistoryValidationError("invalid_history_transaction") from None
    message = _object(transaction.get("message"))
    keys = _addresses(message.get("accountKeys"))
    instructions = message.get("instructions")
    if (
        not keys
        or not isinstance(instructions, list)
        or any(not isinstance(instruction, dict) for instruction in instructions)
    ):
        raise HistoryValidationError("invalid_history_transaction")
    meta = data["meta"]
    if not isinstance(meta, dict) or "err" not in meta:
        raise HistoryValidationError("history_metadata_unavailable")
    error = _transaction_error(meta["err"])
    _validate_amounts(meta, message, keys)
    version = _version(data.get("version"))
    try:
        return HistoryTransaction(
            raw_event_id=raw_event_id,
            page_raw_event_id=page_raw_event_id,
            signature=signatures[0],
            slot=data["slot"],
            transaction_index=data["transactionIndex"],
            block_time=data["blockTime"],
            version=version,
            transaction_error=error,
            payload_json=json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False),
        )
    except (ValidationError, TypeError, ValueError, RecursionError):
        raise HistoryValidationError("invalid_history_transaction") from None


def parse_history_page(response: object, *, limit: int) -> tuple[list[object], str | None]:
    """Validate only the page wrapper; individual records must be journaled first."""
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise HistoryValidationError("invalid_history_request")
    if not isinstance(response, dict) or set(response) != {"result"}:
        raise HistoryValidationError("invalid_history_page")
    result = response["result"]
    if not isinstance(result, dict) or not {"data", "paginationToken"} <= set(result):
        raise HistoryValidationError("invalid_history_page")
    if set(result) - {"data", "paginationToken"}:
        raise HistoryValidationError("unknown_history_page_fields")
    data = result["data"]
    if not isinstance(data, list) or len(data) > limit:
        raise HistoryValidationError("invalid_history_page")
    return list(data), _token(result["paginationToken"])
