"""Repository for canonical event deduplication and finality reconciliation."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import raw_events, reconciliation_records, scout_events
from app.domain.keys import canonical_event_key_for
from app.domain.models import CanonicalSolanaEvent

ReconciliationOutcome = Literal["matched", "changed", "missing", "failed"]
SessionSource = Session | sessionmaker[Session] | Callable[[], Session]
_RECONCILIATION_REASONS: dict[str, str | None] = {
    "matched": None,
    "changed": "finalized canonical fields differ from confirmed observation",
    "missing": "confirmed canonical identity is absent from finalized observation",
    "failed": "finalized lookup or complete decoding was unavailable",
}


class CanonicalEventError(ValueError):
    """Base class for safe canonical-event persistence errors."""


class CanonicalEventKeyError(CanonicalEventError):
    """The event's declared key does not match its canonical identity."""


@dataclass(frozen=True)
class ScoutEventRecord:
    """Persisted canonical event projection."""

    id: int
    event_key: str
    raw_event_id: int
    signature: str
    slot: int
    block_time: datetime | None
    observed_at: datetime
    commitment: str
    program: str
    program_id: str
    event_name: str
    event_type: str
    mint: str | None
    user: str | None
    creator: str | None
    bonding_curve: str | None
    pool: str | None
    quote_mint: str | None
    transaction_index: int | None
    instruction_index: int | None
    event_index: int
    payload_json: str
    decode_status: str
    finality_status: str
    reconciliation_status: str
    derivation_version: str
    created_at: datetime


@dataclass(frozen=True)
class ReconciliationRecord:
    """Persisted comparison of a provisional and finalized raw observation."""

    id: int
    event_key: str
    confirmed_payload_ref: int | None
    finalized_payload_ref: int | None
    status: str
    reason: str | None
    confirmed_slot: int | None
    finalized_slot: int | None
    created_at: datetime
    resolved_at: datetime | None


@dataclass(frozen=True)
class EventReconciliation:
    """One validated comparison result for an existing confirmed observation."""

    event_key: str
    status: ReconciliationOutcome
    finalized_raw_event_id: int | None = None
    finalized_slot: int | None = None


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _record(row: Any, record_type: type[ScoutEventRecord] | type[ReconciliationRecord]) -> Any:
    values = dict(row._mapping if hasattr(row, "_mapping") else row)
    for field in ("block_time", "observed_at", "created_at", "resolved_at"):
        if field in values:
            values[field] = _utc(values[field])
    return record_type(**values)


def _scout_record(row: Any) -> ScoutEventRecord:
    return cast(ScoutEventRecord, _record(row, ScoutEventRecord))


def _reconciliation_record(row: Any) -> ReconciliationRecord:
    return cast(ReconciliationRecord, _record(row, ReconciliationRecord))


def _canonical_payload(event: CanonicalSolanaEvent) -> str:
    return json.dumps(event.payload, sort_keys=True, separators=(",", ":"))


def canonical_events_match(stored: ScoutEventRecord, event: CanonicalSolanaEvent) -> bool:
    """Compare retained canonical identity and fields, excluding observation provenance."""
    return (
        stored.event_key == event.event_key == canonical_event_key_for(event)
        and stored.signature == event.signature
        and stored.slot == event.slot
        and _utc(stored.block_time) == _utc(event.block_time)
        and stored.program == event.program.value
        and stored.program_id == event.program_id
        and stored.event_name == event.event_name
        and stored.event_type == event.event_type.value
        and stored.mint == event.mint
        and stored.user == event.user
        and stored.creator == event.creator
        and stored.bonding_curve == event.bonding_curve
        and stored.pool == event.pool
        and stored.quote_mint == event.quote_mint
        and stored.transaction_index == event.transaction_index
        and stored.instruction_index == event.instruction_index
        and stored.event_index == event.event_index
        and stored.decode_status == event.decode_status.value
        and stored.payload_json == _canonical_payload(event)
    )


def _event_values(
    event: CanonicalSolanaEvent, *, event_key: str, created_at: datetime
) -> dict[str, Any]:
    return {
        "event_key": event_key,
        "raw_event_id": event.raw_event_id,
        "signature": event.signature,
        "slot": event.slot,
        "block_time": event.block_time,
        "observed_at": event.observed_at,
        "commitment": event.commitment.value,
        "program": event.program.value,
        "program_id": event.program_id,
        "event_name": event.event_name,
        "event_type": event.event_type.value,
        "mint": event.mint,
        "user": event.user,
        "creator": event.creator,
        "bonding_curve": event.bonding_curve,
        "pool": event.pool,
        "quote_mint": event.quote_mint,
        "transaction_index": event.transaction_index,
        "instruction_index": event.instruction_index,
        "event_index": event.event_index,
        "payload_json": _canonical_payload(event),
        "decode_status": event.decode_status.value,
        "finality_status": event.finality_status.value,
        "reconciliation_status": event.reconciliation_status.value,
        "derivation_version": event.derivation_version,
        "created_at": created_at,
    }


class CanonicalEventRepository:
    """Persist one canonical event row per identity and append reconciliation records."""

    def __init__(self, session_source: SessionSource) -> None:
        self._session_source = session_source

    def _session(self) -> tuple[Session, bool]:
        if isinstance(self._session_source, Session):
            return self._session_source, False
        return self._session_source(), True

    def insert_if_absent(self, event: CanonicalSolanaEvent) -> ScoutEventRecord:
        """Insert once by canonical key; equivalent observations return the stored row."""
        key = canonical_event_key_for(event)
        if event.event_key != key:
            raise CanonicalEventKeyError("event_key does not match canonical event identity")
        if event.raw_event_id is None:
            raise CanonicalEventError("raw_event_id is required for canonical event persistence")

        session, owns_session = self._session()
        try:
            existing = session.execute(
                select(scout_events).where(scout_events.c.event_key == key)
            ).first()
            if existing is not None:
                session.commit()
                return _scout_record(existing)

            values = _event_values(event, event_key=key, created_at=datetime.now(UTC))
            try:
                session.execute(insert(scout_events).values(values))
                session.commit()
            except IntegrityError:
                session.rollback()
                raced = session.execute(
                    select(scout_events).where(scout_events.c.event_key == key)
                ).first()
                if raced is None:
                    raise
                session.commit()
                return _scout_record(raced)

            persisted = session.execute(
                select(scout_events).where(scout_events.c.event_key == key)
            ).first()
            if persisted is None:
                raise CanonicalEventError("canonical event insert did not produce a persisted row")
            return _scout_record(persisted)
        except Exception:
            if session.in_transaction():
                session.rollback()
            raise
        finally:
            if owns_session:
                session.close()

    def confirmed_for_signature(self, signature: str) -> list[ScoutEventRecord]:
        """Read retained confirmed observations without committing caller transactions."""
        if not isinstance(signature, str) or not signature:
            raise CanonicalEventError("signature must be a non-empty string")
        session, owns_session = self._session()
        try:
            return [
                _scout_record(row)
                for row in session.execute(
                    select(scout_events)
                    .where(
                        scout_events.c.signature == signature,
                        scout_events.c.commitment == "confirmed",
                    )
                    .order_by(scout_events.c.id)
                )
            ]
        finally:
            if owns_session:
                session.close()

    @staticmethod
    def _validate_comparisons(comparisons: Sequence[EventReconciliation]) -> None:
        keys: set[str] = set()
        for comparison in comparisons:
            if (
                not isinstance(comparison.event_key, str)
                or not comparison.event_key
                or comparison.event_key in keys
            ):
                raise CanonicalEventError("reconciliation requires distinct stored event keys")
            keys.add(comparison.event_key)
            if (
                not isinstance(comparison.status, str)
                or comparison.status not in _RECONCILIATION_REASONS
            ):
                raise CanonicalEventError("reconciliation outcome is unsupported")
            for value in (comparison.finalized_raw_event_id, comparison.finalized_slot):
                if value is not None and (type(value) is not int or value < 0):
                    raise CanonicalEventError("finalized reference and slot must be non-negative")
            if comparison.status in {"matched", "changed"} and (
                comparison.finalized_raw_event_id is None or comparison.finalized_slot is None
            ):
                raise CanonicalEventError("finalized comparison requires a raw reference and slot")

    @staticmethod
    def _begin_sqlite_transaction(session: Session) -> None:
        # Legacy sqlite3 does not begin a physical transaction for SELECT or
        # SAVEPOINT. Serialize writers before reading the latest audit attempt,
        # and prevent releasing a savepoint from committing the caller's work.
        connection = session.connection()
        if connection.dialect.name == "sqlite":
            dbapi = connection.connection.driver_connection
            if not getattr(dbapi, "in_transaction", False):
                connection.exec_driver_sql("BEGIN IMMEDIATE")

    @staticmethod
    def _apply_comparison(
        session: Session, comparison: EventReconciliation
    ) -> ReconciliationRecord:
        existing = session.execute(
            select(scout_events)
            .where(scout_events.c.event_key == comparison.event_key)
            .with_for_update()
        ).first()
        if existing is None or existing._mapping["commitment"] != "confirmed":
            raise CanonicalEventError(
                "cannot reconcile finalized event without a confirmed canonical row"
            )
        stored = existing._mapping
        if (
            comparison.finalized_raw_event_id is not None
            and session.execute(
                select(raw_events.c.id).where(raw_events.c.id == comparison.finalized_raw_event_id)
            ).first()
            is None
        ):
            raise CanonicalEventError("finalized raw reference must be persisted first")

        values = {
            "event_key": comparison.event_key,
            "confirmed_payload_ref": stored["raw_event_id"],
            "finalized_payload_ref": comparison.finalized_raw_event_id,
            "status": comparison.status,
            "reason": _RECONCILIATION_REASONS[comparison.status],
            "confirmed_slot": stored["slot"],
            "finalized_slot": comparison.finalized_slot,
        }
        latest = session.execute(
            select(reconciliation_records)
            .where(reconciliation_records.c.event_key == comparison.event_key)
            .order_by(reconciliation_records.c.id.desc())
            .limit(1)
        ).first()
        if latest is not None and all(
            latest._mapping[key] == value for key, value in values.items()
        ):
            return _reconciliation_record(latest)

        if comparison.status != "failed":
            session.execute(
                update(scout_events)
                .where(scout_events.c.event_key == comparison.event_key)
                .values(
                    finality_status="finalized"
                    if comparison.status == "matched"
                    else "reorged_or_changed",
                    reconciliation_status=comparison.status,
                )
            )
        now = datetime.now(UTC)
        reconciliation_id: int = session.execute(
            insert(reconciliation_records)
            .values(
                **values,
                created_at=now,
                resolved_at=None if comparison.status == "failed" else now,
            )
            .returning(reconciliation_records.c.id)
        ).scalar_one()
        reconciliation = session.execute(
            select(reconciliation_records).where(reconciliation_records.c.id == reconciliation_id)
        ).first()
        if reconciliation is None:
            raise CanonicalEventError("reconciliation did not produce a persisted record")
        return _reconciliation_record(reconciliation)

    def apply_reconciliations(
        self, comparisons: Sequence[EventReconciliation]
    ) -> list[ReconciliationRecord]:
        """Atomically append comparisons; caller sessions retain transaction ownership."""
        self._validate_comparisons(comparisons)
        if not comparisons:
            return []
        session, owns_session = self._session()
        try:
            if not owns_session:
                self._begin_sqlite_transaction(session)
            with session.begin() if owns_session else session.begin_nested():
                if owns_session:
                    self._begin_sqlite_transaction(session)
                return [self._apply_comparison(session, comparison) for comparison in comparisons]
        finally:
            if owns_session:
                session.close()

    def reconcile_finalized(
        self, event: CanonicalSolanaEvent, *, finalized_raw_event_id: int | None = None
    ) -> tuple[ScoutEventRecord | None, ReconciliationRecord]:
        """Compare a finalized copy without replacing the original canonical observation."""
        key = canonical_event_key_for(event)
        if event.event_key != key:
            raise CanonicalEventKeyError("event_key does not match canonical event identity")
        if event.commitment.value != "finalized":
            raise CanonicalEventError("finalized reconciliation requires a finalized event")

        session, owns_session = self._session()
        try:
            if not owns_session:
                self._begin_sqlite_transaction(session)
            with session.begin() if owns_session else session.begin_nested():
                if owns_session:
                    self._begin_sqlite_transaction(session)
                existing = session.execute(
                    select(scout_events).where(scout_events.c.event_key == key).with_for_update()
                ).first()
                if existing is None:
                    raise CanonicalEventError(
                        "cannot reconcile finalized event without a confirmed canonical row"
                    )
                comparison = EventReconciliation(
                    event_key=key,
                    status="matched"
                    if canonical_events_match(_scout_record(existing), event)
                    else "changed",
                    finalized_raw_event_id=finalized_raw_event_id
                    if finalized_raw_event_id is not None
                    else event.raw_event_id,
                    finalized_slot=event.slot,
                )
                self._validate_comparisons([comparison])
                reconciliation = self._apply_comparison(session, comparison)
                updated = session.execute(
                    select(scout_events).where(scout_events.c.event_key == key)
                ).first()
                if updated is None:
                    raise CanonicalEventError("reconciliation did not produce a persisted row")
                return _scout_record(updated), reconciliation
        finally:
            if owns_session:
                session.close()
