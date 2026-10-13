"""Repository for canonical event deduplication and finality reconciliation."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import reconciliation_records, scout_events
from app.domain.keys import canonical_event_key_for
from app.domain.models import CanonicalSolanaEvent

ReconciliationOutcome = Literal["matched", "changed", "missing", "failed"]
SessionSource = Session | sessionmaker[Session] | Callable[[], Session]


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
            existing = session.execute(
                select(scout_events).where(scout_events.c.event_key == key)
            ).first()
            now = datetime.now(UTC)
            finalized_ref = finalized_raw_event_id or event.raw_event_id
            if existing is None:
                raise CanonicalEventError(
                    "cannot reconcile finalized event without a confirmed canonical row"
                )

            stored = existing._mapping
            stored_payload = stored["payload_json"]
            finalized_payload = _canonical_payload(event)
            matched = (
                stored["signature"] == event.signature
                and stored["slot"] == event.slot
                and stored["program_id"] == event.program_id
                and stored["event_name"] == event.event_name
                and stored["event_index"] == event.event_index
                and stored_payload == finalized_payload
            )
            status: ReconciliationOutcome = "matched" if matched else "changed"
            reason = (
                None
                if matched
                else "finalized canonical payload differs from confirmed observation"
            )
            confirmed_ref = stored["raw_event_id"]
            session.execute(
                update(scout_events)
                .where(scout_events.c.event_key == key)
                .values(
                    finality_status="finalized" if matched else "reorged_or_changed",
                    reconciliation_status=status,
                )
            )
            reconciliation_id: int = session.execute(
                insert(reconciliation_records)
                .values(
                    event_key=key,
                    confirmed_payload_ref=confirmed_ref,
                    finalized_payload_ref=finalized_ref,
                    status=status,
                    reason=reason,
                    confirmed_slot=stored["slot"],
                    finalized_slot=event.slot,
                    created_at=now,
                    resolved_at=now,
                )
                .returning(reconciliation_records.c.id)
            ).scalar_one()
            session.commit()
            updated = session.execute(
                select(scout_events).where(scout_events.c.event_key == key)
            ).first()
            reconciliation = session.execute(
                select(reconciliation_records).where(
                    reconciliation_records.c.id == reconciliation_id
                )
            ).first()
            if updated is None or reconciliation is None:
                raise CanonicalEventError("reconciliation did not produce persisted rows")
            return _scout_record(updated), _reconciliation_record(reconciliation)
        except Exception:
            if session.in_transaction():
                session.rollback()
            raise
        finally:
            if owns_session:
                session.close()
