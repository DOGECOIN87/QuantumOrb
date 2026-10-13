"""Deterministic identity keys for raw notifications and canonical events."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from app.domain.enums import Transport

if TYPE_CHECKING:
    from app.domain.models import CanonicalSolanaEvent


class EventKeyError(ValueError):
    """Raised when an event identity component is invalid."""


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise EventKeyError(f"{field} must be a non-empty string")
    return value


def _non_negative_index(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EventKeyError("event_index must be a non-negative integer")
    return value


def _sha256_identity(identity: str) -> str:
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def raw_notification_key(provider: str, transport: str | Transport, notification_id: str) -> str:
    """Return the stable key for one provider/transport notification identity."""
    provider_text = _required_text(provider, "provider")
    transport_text = _required_text(
        transport.value if isinstance(transport, Transport) else transport, "transport"
    )
    notification_text = _required_text(notification_id, "notification_id")
    return _sha256_identity("\x1f".join((provider_text, transport_text, notification_text)))


def canonical_event_key(
    provider: str,
    signature: str,
    program_id: str,
    event_name: str,
    event_index: int,
) -> str:
    """Return the commitment-independent key for one canonical event.

    Commitment, transport, slot, and payload are intentionally excluded so confirmed
    and finalized observations of the same event share one canonical identity.
    """
    components = (
        _required_text(provider, "provider"),
        _required_text(signature, "signature"),
        _required_text(program_id, "program_id"),
        _required_text(event_name, "event_name"),
        str(_non_negative_index(event_index)),
    )
    return _sha256_identity(":".join(components))


def canonical_event_key_for(event: CanonicalSolanaEvent) -> str:
    """Derive a canonical key from a validated canonical event record."""
    return canonical_event_key(
        "helius", event.signature, event.program_id, event.event_name, event.event_index
    )
