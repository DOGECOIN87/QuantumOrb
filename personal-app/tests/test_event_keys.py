"""Offline tests for deterministic T007 identity keys."""

from __future__ import annotations

import hashlib
from datetime import UTC

import pytest

from app.domain.keys import (
    EventKeyError,
    canonical_event_key,
    canonical_event_key_for,
    raw_notification_key,
)
from app.domain.models import CanonicalSolanaEvent

SIGNATURE = "1" * 64


def test_raw_notification_key_matches_contract_and_is_stable() -> None:
    expected = hashlib.sha256(b"helius\x1ffixture\x1fnotification-001").hexdigest()
    assert raw_notification_key("helius", "fixture", "notification-001") == expected
    assert raw_notification_key("helius", "fixture", "notification-001") == expected
    assert raw_notification_key("helius", "fixture", "notification-001") != raw_notification_key(
        "helius", "fixture", "notification-002"
    )


def test_raw_notification_key_accepts_transport_enum() -> None:
    assert raw_notification_key("helius", "fixture", "notification-001") == raw_notification_key(
        "helius", "fixture", "notification-001"
    )


def test_canonical_key_matches_contract() -> None:
    identity = "helius:" + ":".join((SIGNATURE, "pump-program", "CreateEvent", "0"))
    expected = hashlib.sha256(identity.encode()).hexdigest()
    assert canonical_event_key("helius", SIGNATURE, "pump-program", "CreateEvent", 0) == expected


def test_confirmed_and_finalized_copies_share_canonical_key() -> None:
    confirmed = canonical_event_key("helius", SIGNATURE, "pump-program", "TradeEvent", 3)
    finalized = canonical_event_key("helius", SIGNATURE, "pump-program", "TradeEvent", 3)
    assert confirmed == finalized


def test_canonical_key_from_validated_event_ignores_commitment_and_payload() -> None:
    common = {
        "eventKey": "a" * 64,
        "signature": SIGNATURE,
        "slot": 42,
        "observedAt": "2026-01-02T03:04:05Z",
        "program": "pump",
        "programId": "pump-program",
        "eventName": "TradeEvent",
        "eventType": "curve_buy",
        "payload": {"amount": "1"},
        "decodeStatus": "decoded",
        "finalityStatus": "provisional",
        "reconciliationStatus": "pending",
        "derivationVersion": "t007",
    }
    confirmed = CanonicalSolanaEvent(commitment="confirmed", **common)
    finalized = CanonicalSolanaEvent(
        commitment="finalized",
        **{**common, "payload": {"amount": "2"}, "slot": 43},
    )
    assert canonical_event_key_for(confirmed) == canonical_event_key_for(finalized)
    assert confirmed.observed_at.tzinfo == UTC


@pytest.mark.parametrize(
    ("function", "args"),
    [
        (raw_notification_key, ("", "fixture", "id")),
        (raw_notification_key, ("helius", "", "id")),
        (raw_notification_key, ("helius", "fixture", "")),
        (canonical_event_key, ("helius", "sig", "program", "event", -1)),
        (canonical_event_key, ("helius", "sig", "program", "event", True)),
    ],
)
def test_key_helpers_reject_invalid_identity_components(
    function: object, args: tuple[object, ...]
) -> None:
    with pytest.raises(EventKeyError):
        function(*args)  # type: ignore[operator]
