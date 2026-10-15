"""Atomic, append-only finalized comparison persistence without live provider calls."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import func, insert, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.canonical_events import (
    CanonicalEventError,
    CanonicalEventRepository,
    EventReconciliation,
    ReconciliationOutcome,
    canonical_events_match,
)
from app.db.models import raw_events, reconciliation_records, scout_events
from app.db.session import create_database_engine
from app.domain.enums import DecodeStatus, ProgramName, ScoutEventType
from app.domain.keys import canonical_event_key
from app.domain.models import CanonicalSolanaEvent

SIGNATURE = "1" * 64
OTHER_SIGNATURE = "1" * 63 + "2"
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def event(**changes: Any) -> CanonicalSolanaEvent:
    values: dict[str, Any] = {
        "event_key": canonical_event_key("helius", SIGNATURE, "pump-program", "TradeEvent", 0),
        "signature": SIGNATURE,
        "slot": 42,
        "block_time": NOW,
        "observed_at": NOW,
        "commitment": "confirmed",
        "program": "pump",
        "program_id": "pump-program",
        "event_name": "TradeEvent",
        "event_type": "curve_buy",
        "mint": "mint",
        "user": "user",
        "creator": "creator",
        "bonding_curve": "curve",
        "pool": "pool",
        "quote_mint": "quote",
        "transaction_index": 0,
        "instruction_index": 0,
        "event_index": 0,
        "payload": {"amount": "100", "second": "200"},
        "decode_status": "decoded",
        "finality_status": "provisional",
        "reconciliation_status": "pending",
        "derivation_version": "test",
        "raw_event_id": 1,
    }
    values.update(changes)
    return CanonicalSolanaEvent.model_validate(values)


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_database_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        for raw_id, commitment in ((1, "confirmed"), (2, "finalized")):
            payload_json = json.dumps({"source": commitment}, sort_keys=True)
            session.execute(
                insert(raw_events).values(
                    id=raw_id,
                    event_key=str(raw_id).zfill(64),
                    provider="helius",
                    transport="fixture",
                    notification_id=str(raw_id),
                    signature=SIGNATURE,
                    slot=42,
                    commitment=commitment,
                    received_at=NOW,
                    payload_json=payload_json,
                    payload_sha256=hashlib.sha256(payload_json.encode()).hexdigest(),
                    status="new",
                    created_at=NOW,
                )
            )
        session.commit()
        CanonicalEventRepository(session).insert_if_absent(event())
        yield session
    engine.dispose()


def comparison(status: ReconciliationOutcome = "matched") -> EventReconciliation:
    return EventReconciliation(event().event_key, status, 2, 42)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_key", "f" * 64),
        ("signature", OTHER_SIGNATURE),
        ("slot", 43),
        ("block_time", NOW + timedelta(seconds=1)),
        ("program", ProgramName.PUMP_AMM),
        ("program_id", "other-program"),
        ("event_name", "other-event"),
        ("event_type", ScoutEventType.CURVE_SELL),
        ("mint", "other-mint"),
        ("user", "other-user"),
        ("creator", "other-creator"),
        ("bonding_curve", "other-curve"),
        ("pool", "other-pool"),
        ("quote_mint", "other-quote"),
        ("transaction_index", 1),
        ("instruction_index", 1),
        ("event_index", 1),
        ("decode_status", DecodeStatus.RAW_ONLY),
        ("payload", {"amount": "999"}),
    ],
)
def test_match_compares_every_retained_canonical_field(
    db: Session, field: str, value: object
) -> None:
    stored = CanonicalEventRepository(db).confirmed_for_signature(SIGNATURE)[0]
    assert not canonical_events_match(stored, event().model_copy(update={field: value}))


def test_match_excludes_provenance_and_handles_equivalent_payload_order(db: Session) -> None:
    stored = CanonicalEventRepository(db).confirmed_for_signature(SIGNATURE)[0]
    finalized = event(
        commitment="finalized",
        observed_at=NOW + timedelta(days=1),
        finality_status="finalized",
        reconciliation_status="not_required",
        derivation_version="new-version",
        raw_event_id=2,
        payload={"second": "200", "amount": "100"},
    )
    assert canonical_events_match(stored, finalized)


@pytest.mark.parametrize("status", ["matched", "changed", "missing", "failed"])
def test_all_outcomes_preserve_original_observation(db: Session, status: str) -> None:
    repository = CanonicalEventRepository(db)
    original = repository.confirmed_for_signature(SIGNATURE)[0]
    record = repository.apply_reconciliations([comparison(cast(ReconciliationOutcome, status))])[0]
    updated = repository.confirmed_for_signature(SIGNATURE)[0]
    before, after = asdict(original), asdict(updated)
    for field in ("finality_status", "reconciliation_status"):
        before.pop(field)
        after.pop(field)
    assert before == after
    expected = {
        "matched": ("finalized", "matched"),
        "changed": ("reorged_or_changed", "changed"),
        "missing": ("reorged_or_changed", "missing"),
        "failed": ("provisional", "pending"),
    }
    assert (updated.finality_status, updated.reconciliation_status) == expected[status]
    assert record.status == status
    assert record.confirmed_payload_ref == 1
    assert record.finalized_payload_ref == 2
    assert record.confirmed_slot == record.finalized_slot == 42
    assert (record.resolved_at is None) == (status == "failed")
    assert record.created_at.tzinfo is UTC
    assert db.scalar(select(func.count()).select_from(scout_events)) == 1


@pytest.mark.parametrize("status", ["matched", "changed", "missing", "failed"])
def test_latest_identical_attempt_is_idempotent(db: Session, status: str) -> None:
    repository = CanonicalEventRepository(db)
    attempt = comparison(cast(ReconciliationOutcome, status))
    first = repository.apply_reconciliations([attempt])[0]
    second = repository.apply_reconciliations([attempt])[0]
    assert first == second
    assert db.scalar(select(func.count()).select_from(reconciliation_records)) == 1


def test_recovery_and_changed_results_append_audit_history(db: Session) -> None:
    repository = CanonicalEventRepository(db)
    statuses: tuple[ReconciliationOutcome, ...] = (
        "failed",
        "matched",
        "changed",
        "failed",
        "matched",
    )
    records = [repository.apply_reconciliations([comparison(status)])[0] for status in statuses]
    assert len({record.id for record in records}) == 5
    assert records[0].resolved_at is None
    assert records[3].resolved_at is None
    assert db.scalar(select(func.count()).select_from(reconciliation_records)) == 5
    assert repository.confirmed_for_signature(SIGNATURE)[0].finality_status == "finalized"


@pytest.mark.parametrize("status", ["matched", "changed", "missing"])
def test_failed_attempt_preserves_prior_terminal_flags(db: Session, status: str) -> None:
    repository = CanonicalEventRepository(db)
    repository.apply_reconciliations([comparison(cast(ReconciliationOutcome, status))])
    original = repository.confirmed_for_signature(SIGNATURE)[0]
    repository.apply_reconciliations([comparison("failed")])
    assert repository.confirmed_for_signature(SIGNATURE)[0] == original


def test_authoritative_missing_and_failed_can_have_no_finalized_reference(db: Session) -> None:
    repository = CanonicalEventRepository(db)
    for status in ("missing", "failed"):
        record = repository.apply_reconciliations([EventReconciliation(event().event_key, status)])[
            0
        ]
        assert record.finalized_payload_ref is None
        assert record.finalized_slot is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "unsupported"),
        ("event_key", ""),
        ("finalized_raw_event_id", -1),
        ("finalized_raw_event_id", True),
        ("finalized_raw_event_id", "2"),
        ("finalized_slot", -1),
        ("finalized_slot", True),
        ("finalized_slot", "42"),
        ("finalized_raw_event_id", None),
        ("finalized_slot", None),
    ],
)
def test_invalid_comparison_rejected_before_mutation(db: Session, field: str, value: Any) -> None:
    with pytest.raises(CanonicalEventError):
        CanonicalEventRepository(db).apply_reconciliations(
            [replace(comparison(), **{field: value})]
        )
    assert db.scalar(select(func.count()).select_from(reconciliation_records)) == 0
    assert CanonicalEventRepository(db).confirmed_for_signature(SIGNATURE)[0].finality_status == (
        "provisional"
    )


def test_duplicate_batch_keys_and_missing_raw_reference_rejected(db: Session) -> None:
    repository = CanonicalEventRepository(db)
    with pytest.raises(CanonicalEventError, match="distinct"):
        repository.apply_reconciliations([comparison(), comparison("changed")])
    with pytest.raises(CanonicalEventError, match="persisted first"):
        repository.apply_reconciliations([replace(comparison(), finalized_raw_event_id=99)])
    assert db.scalar(select(func.count()).select_from(reconciliation_records)) == 0


def test_batch_failure_rolls_back_only_its_savepoint(db: Session) -> None:
    db.execute(update(scout_events).values(user="caller-change"))
    repository = CanonicalEventRepository(db)
    with pytest.raises(CanonicalEventError, match="without a confirmed canonical row"):
        repository.apply_reconciliations([comparison(), EventReconciliation("f" * 64, "missing")])
    stored = repository.confirmed_for_signature(SIGNATURE)[0]
    assert stored.user == "caller-change"
    assert stored.finality_status == "provisional"
    assert db.scalar(select(func.count()).select_from(reconciliation_records)) == 0
    db.rollback()
    assert repository.confirmed_for_signature(SIGNATURE)[0].user == "user"


def test_caller_rollback_undoes_successful_comparison_and_preserves_original(db: Session) -> None:
    repository = CanonicalEventRepository(db)
    repository.apply_reconciliations([comparison()])
    assert repository.confirmed_for_signature(SIGNATURE)[0].finality_status == "finalized"
    db.rollback()
    assert repository.confirmed_for_signature(SIGNATURE)[0].finality_status == "provisional"
    assert db.scalar(select(func.count()).select_from(reconciliation_records)) == 0


def test_factory_sessions_commit_and_failed_batch_rolls_back(db: Session) -> None:
    factory = sessionmaker(db.get_bind())
    repository = CanonicalEventRepository(factory)
    with pytest.raises(CanonicalEventError):
        repository.apply_reconciliations([comparison(), EventReconciliation("f" * 64, "missing")])
    assert db.scalar(select(func.count()).select_from(reconciliation_records)) == 0
    repository.apply_reconciliations([comparison()])
    assert db.scalar(select(func.count()).select_from(reconciliation_records)) == 1
    assert repository.confirmed_for_signature(SIGNATURE)[0].finality_status == "finalized"


def test_confirmed_reader_excludes_other_commitments_and_signatures_without_commit(
    db: Session,
) -> None:
    repository = CanonicalEventRepository(db)
    repository.insert_if_absent(
        event(
            signature=OTHER_SIGNATURE,
            event_key=canonical_event_key(
                "helius", OTHER_SIGNATURE, "pump-program", "TradeEvent", 0
            ),
        )
    )
    repository.insert_if_absent(
        event(
            commitment="finalized",
            event_index=1,
            event_key=canonical_event_key("helius", SIGNATURE, "pump-program", "TradeEvent", 1),
        )
    )
    db.execute(
        update(scout_events).where(scout_events.c.signature == SIGNATURE).values(user="edit")
    )
    assert len(repository.confirmed_for_signature(SIGNATURE)) == 1
    assert len(repository.confirmed_for_signature(OTHER_SIGNATURE)) == 1
    assert repository.confirmed_for_signature("unknown") == []
    db.rollback()
    assert repository.confirmed_for_signature(SIGNATURE)[0].user == "user"
    with pytest.raises(CanonicalEventError, match="signature"):
        repository.confirmed_for_signature("")


def test_finalized_only_original_cannot_be_reconciled(db: Session) -> None:
    db.execute(update(scout_events).values(commitment="finalized"))
    with pytest.raises(CanonicalEventError, match="confirmed canonical row"):
        CanonicalEventRepository(db).apply_reconciliations([comparison()])
    assert db.scalar(select(func.count()).select_from(reconciliation_records)) == 0


def test_empty_batch_does_not_start_a_transaction(db: Session) -> None:
    db.rollback()
    assert CanonicalEventRepository(db).apply_reconciliations([]) == []
    assert not db.in_transaction()


def test_compatibility_reconciliation_uses_all_fields_and_is_idempotent(db: Session) -> None:
    repository = CanonicalEventRepository(db)
    finalized = event(commitment="finalized", raw_event_id=2, mint="changed-mint")
    first, record = repository.reconcile_finalized(finalized)
    second, repeated = repository.reconcile_finalized(finalized)
    assert first == second
    assert record == repeated
    assert first is not None and first.mint == "mint"
    assert record.status == "changed"
    db.rollback()
    assert db.scalar(select(func.count()).select_from(reconciliation_records)) == 0


def test_explicit_zero_reference_does_not_fall_back_to_event_reference(db: Session) -> None:
    with pytest.raises(CanonicalEventError, match="persisted first"):
        CanonicalEventRepository(db).reconcile_finalized(
            event(commitment="finalized", raw_event_id=2), finalized_raw_event_id=0
        )
    assert db.scalar(select(func.count()).select_from(reconciliation_records)) == 0
