"""Offline acceptance tests for the immutable raw event journal."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import raw_events
from app.db.raw_journal import (
    RawJournal,
    RawJournalConflictError,
    RawJournalHashError,
    event_key_for,
)
from app.domain.models import ProviderEnvelope

VALID_SIGNATURE = "1" * 64


def make_envelope(
    payload: dict[str, object] | None = None,
    *,
    notification_id: str = "notification-001",
    raw_hash: str | None = None,
) -> ProviderEnvelope:
    raw_payload = payload or {
        "event": "create",
        "amount": "18446744073709551615",
        "nested": {"z": 2, "a": 1},
    }
    canonical = json.dumps(raw_payload, sort_keys=True, separators=(",", ":"))
    return ProviderEnvelope(
        transport="fixture",
        notification_id=notification_id,
        received_at="2026-01-02T03:04:05+00:00",
        commitment="confirmed",
        slot=42,
        signature=VALID_SIGNATURE,
        transaction_error={"code": "provider-error"},
        raw_payload_sha256=raw_hash or hashlib.sha256(canonical.encode()).hexdigest(),
        idl_revision="pump-2026-01",
        raw_payload=raw_payload,
    )


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session


def test_valid_insertion_round_trips_payload_hash_and_status(db: Session) -> None:
    envelope = make_envelope()
    row = RawJournal(db).insert(envelope)

    assert row.status == "new"
    assert row.provider == "helius"
    assert json.loads(row.payload_json) == envelope.raw_payload
    assert row.payload_sha256 == hashlib.sha256(row.payload_json.encode()).hexdigest()
    assert row.payload_sha256 == row.payload_sha256.lower()
    assert len(row.payload_sha256) == 64
    assert row.received_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert row.received_at.tzinfo is not None
    assert json.loads(row.payload_json)["amount"] == "18446744073709551615"
    assert db.scalar(select(func.count()).select_from(raw_events)) == 1


def test_same_identity_and_hash_is_a_successful_noop(db: Session) -> None:
    journal = RawJournal(db)
    first = journal.insert(make_envelope())
    second = journal.insert(make_envelope())

    assert second.id == first.id
    assert db.scalar(select(func.count()).select_from(raw_events)) == 1


def test_same_identity_with_different_hash_never_overwrites(db: Session) -> None:
    journal = RawJournal(db)
    first = journal.insert(make_envelope())
    with pytest.raises(RawJournalConflictError, match="different payload hash"):
        journal.insert(make_envelope({"event": "changed", "amount": "999"}))

    persisted = db.execute(select(raw_events).where(raw_events.c.id == first.id)).one()._mapping
    assert json.loads(persisted["payload_json"])["event"] == "create"
    assert db.scalar(select(func.count()).select_from(raw_events)) == 1


def test_forged_hash_is_rejected_before_database_mutation(db: Session) -> None:
    with pytest.raises((ValidationError, RawJournalHashError)):
        RawJournal(db).insert(make_envelope(raw_hash="0" * 64))
    assert db.scalar(select(func.count()).select_from(raw_events)) == 0


def test_domain_boundary_rejects_negative_boolean_and_invalid_fields() -> None:
    model = make_envelope().model_dump()
    with pytest.raises(ValidationError):
        ProviderEnvelope(**{**model, "slot": -1})
    with pytest.raises(ValidationError):
        ProviderEnvelope(**{**model, "slot": True})
    with pytest.raises(ValidationError):
        ProviderEnvelope(**{**model, "received_at": "2026-01-02T03:04:05"})
    with pytest.raises(ValidationError):
        ProviderEnvelope(**{**model, "transport": "unknown"})


def test_event_key_is_deterministic_hash_of_contract_identity() -> None:
    envelope = make_envelope()
    expected_identity = "helius\x1ffixture\x1fnotification-001"
    assert event_key_for(envelope) == hashlib.sha256(expected_identity.encode()).hexdigest()
    assert event_key_for(envelope) == event_key_for(make_envelope())
    assert len(event_key_for(envelope)) == 64


def test_transaction_error_stays_at_typed_boundary(db: Session) -> None:
    envelope = make_envelope()
    assert envelope.transaction_error == {"code": "provider-error"}
    row = RawJournal(db).insert(envelope)
    assert "provider-error" not in row.payload_json
    assert row.safe_error_code is None
    assert row.safe_error_message is None
