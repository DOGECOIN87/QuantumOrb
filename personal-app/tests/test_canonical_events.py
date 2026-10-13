"""Offline tests for canonical event persistence and finality reconciliation."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.canonical_events import (
    CanonicalEventKeyError,
    CanonicalEventRepository,
)
from app.db.models import raw_events, reconciliation_records, scout_events
from app.domain.enums import Commitment
from app.domain.keys import canonical_event_key
from app.domain.models import CanonicalSolanaEvent

SIGNATURE = "1" * 64


def make_raw_event(session: Session, event_id: int = 1) -> None:
    payload = {"notification": "raw"}
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    session.execute(
        raw_events.insert().values(
            id=event_id,
            event_key=f"raw-{event_id}".ljust(64, "0"),
            provider="helius",
            transport="fixture",
            notification_id=f"notification-{event_id}",
            signature=SIGNATURE,
            slot=42,
            commitment="confirmed",
            received_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
            payload_json=payload_json,
            payload_sha256=hashlib.sha256(payload_json.encode()).hexdigest(),
            status="new",
            created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        )
    )
    session.commit()


def make_event(
    *,
    commitment: Commitment = Commitment.CONFIRMED,
    payload: dict[str, str] | None = None,
    slot: int = 42,
    raw_event_id: int = 1,
) -> CanonicalSolanaEvent:
    key = canonical_event_key("helius", SIGNATURE, "pump-program", "TradeEvent", 0)
    return CanonicalSolanaEvent(
        eventKey=key,
        signature=SIGNATURE,
        slot=slot,
        observedAt="2026-01-02T03:04:05Z",
        commitment=commitment,
        program="pump",
        programId="pump-program",
        eventName="TradeEvent",
        eventType="curve_buy",
        payload=payload or {"amount": "100"},
        decodeStatus="decoded",
        finalityStatus="provisional",
        reconciliationStatus="pending",
        derivationVersion="t008",
        rawEventId=raw_event_id,
    )


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        make_raw_event(session)
        yield session


def test_equivalent_canonical_events_create_one_row(db: Session) -> None:
    repository = CanonicalEventRepository(db)
    first = repository.insert_if_absent(make_event())
    second = repository.insert_if_absent(make_event())

    assert second.id == first.id
    assert db.scalar(select(func.count()).select_from(scout_events)) == 1


def test_key_mismatch_is_rejected_before_database_mutation(db: Session) -> None:
    event = make_event().model_copy(update={"event_key": "f" * 64})
    with pytest.raises(CanonicalEventKeyError):
        CanonicalEventRepository(db).insert_if_absent(event)
    assert db.scalar(select(func.count()).select_from(scout_events)) == 0


def test_identical_finalized_copy_updates_existing_row_and_appends_match(db: Session) -> None:
    repository = CanonicalEventRepository(db)
    repository.insert_if_absent(make_event())
    finalized, reconciliation = repository.reconcile_finalized(
        make_event(commitment=Commitment.FINALIZED)
    )

    assert finalized is not None
    assert finalized.finality_status == "finalized"
    assert finalized.reconciliation_status == "matched"
    assert reconciliation.status == "matched"
    assert db.scalar(select(func.count()).select_from(scout_events)) == 1
    assert db.scalar(select(func.count()).select_from(reconciliation_records)) == 1


def test_changed_finalized_copy_preserves_original_payload_and_marks_change(db: Session) -> None:
    repository = CanonicalEventRepository(db)
    original = repository.insert_if_absent(make_event())
    finalized, reconciliation = repository.reconcile_finalized(
        make_event(commitment=Commitment.FINALIZED, payload={"amount": "999"})
    )

    assert finalized is not None
    assert finalized.id == original.id
    assert finalized.finality_status == "reorged_or_changed"
    assert finalized.reconciliation_status == "changed"
    assert reconciliation.status == "changed"
    assert reconciliation.reason is not None
    stored_payload = db.execute(
        select(scout_events.c.payload_json).where(scout_events.c.event_key == original.event_key)
    ).scalar_one()
    assert json.loads(stored_payload) == {"amount": "100"}
    assert db.scalar(select(func.count()).select_from(scout_events)) == 1


def test_finalized_reconciliation_requires_finalized_commitment(db: Session) -> None:
    with pytest.raises(ValueError, match="finalized event"):
        CanonicalEventRepository(db).reconcile_finalized(make_event())


def test_missing_confirmed_identity_is_rejected_without_orphan_record(db: Session) -> None:
    event = make_event(commitment=Commitment.FINALIZED)
    with pytest.raises(ValueError, match="without a confirmed canonical row"):
        CanonicalEventRepository(db).reconcile_finalized(event)
    assert db.scalar(select(func.count()).select_from(reconciliation_records)) == 0


def test_raw_event_reference_is_required(db: Session) -> None:
    event = make_event().model_copy(update={"raw_event_id": None})
    with pytest.raises(ValueError, match="raw_event_id"):
        CanonicalEventRepository(db).insert_if_absent(event)
