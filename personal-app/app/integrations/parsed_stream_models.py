"""Validated discovery records and lossless Parsed Streams journal envelopes."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from typing import Any

from pydantic import Field, ValidationError, field_validator

from app.config import _validate_base58_address
from app.domain.enums import Commitment, Transport
from app.domain.models import DomainModel, ProviderEnvelope


class ParsedStreamModelError(ValueError):
    """Value-free diagnostic for an invalid provider identity or JSON payload."""


class ProgramDescription(DomainModel):
    """The exact names returned by a successful provider describeProgram request."""

    id: str = Field(strict=True)
    name: str = Field(strict=True)
    instructions: list[str]
    events: list[str]
    roles: list[str]

    _validate_address = field_validator("id")(_validate_base58_address)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", value) is None:
            raise ValueError("program name is invalid")
        return value

    @field_validator("instructions", "events", "roles", mode="before")
    @classmethod
    def validate_names(cls, value: object) -> object:
        if (
            not isinstance(value, list)
            or any(
                not isinstance(item, str)
                or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", item) is None
                for item in value
            )
            or len(set(value)) != len(value)
        ):
            raise ValueError("discovered names must be unique valid identifiers")
        return value


def _json_value(value: object) -> bool:
    if value is None or type(value) in {str, int, bool}:
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(type(key) is str and _json_value(item) for key, item in value.items())
    return False


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ParsedStreamModelError("parsed stream notification identity is invalid")
    return value


def notification_envelope(
    payload: dict[str, Any],
    *,
    subscription_id: int,
    received_at: datetime,
    idl_revision: str,
) -> ProviderEnvelope:
    """Extract identity only, retaining unknown and malformed bodies for later decode.

    The journal must receive the entire notification before schema-specific
    decoding. A payload hash identifies raw messages, preserving distinct bodies
    for the same transaction while making exact replays idempotent.
    """
    try:
        if (
            type(subscription_id) is not int
            or subscription_id < 0
            or not isinstance(payload, dict)
            or not _json_value(payload)
            or payload.get("jsonrpc") != "2.0"
            or payload.get("method") != "parsedTransactionNotification"
            or not isinstance(idl_revision, str)
            or not idl_revision
            or idl_revision != idl_revision.strip()
            or len(idl_revision) > 128
        ):
            raise ParsedStreamModelError("parsed stream notification identity is invalid")
        params = _object(payload.get("params"))
        subscription = params.get("subscription")
        result = _object(params.get("result"))
        context = _object(result.get("context"))
        value = _object(result.get("value"))
        transaction = _object(value.get("transaction"))
        slot = context.get("slot")
        transaction_slot = transaction.get("slot")
        if (
            type(subscription) is not int
            or subscription != subscription_id
            or type(slot) is not int
            or slot < 0
            or type(transaction_slot) is not int
            or transaction_slot != slot
            or not isinstance(transaction.get("signature"), str)
        ):
            raise ParsedStreamModelError("parsed stream notification identity is invalid")
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        return ProviderEnvelope(
            provider="helius",
            transport=Transport.PARSED_STREAMS,
            notification_id=f"parsed-streams:{digest}",
            received_at=received_at,
            commitment=Commitment.CONFIRMED,
            slot=slot,
            signature=transaction["signature"],
            transaction_error=transaction.get("error"),
            raw_payload_sha256=digest,
            idl_revision=idl_revision,
            raw_payload=json.loads(serialized),
        )
    except (ValidationError, TypeError, ValueError, RecursionError, OverflowError):
        raise ParsedStreamModelError("parsed stream notification identity is invalid") from None
