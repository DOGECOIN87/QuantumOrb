"""Repository boundary for immutable provider envelopes.

The journal owns its transaction: a successful insert or duplicate lookup is committed
before this method returns. No decoder is called here, so callers can safely parse only
after the returned record has been persisted.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import raw_events
from app.domain.models import ProviderEnvelope


class RawJournalError(ValueError):
    """Base class for safe journal-boundary errors."""


class RawJournalHashError(RawJournalError):
    """The supplied envelope hash does not match its retained raw payload."""


class RawJournalConflictError(RawJournalError):
    """An identity already exists with a different immutable payload."""


@dataclass(frozen=True)
class RawEventRecord:
    """Persisted raw-event values returned by the repository."""

    id: int
    event_key: str
    provider: str
    transport: str
    notification_id: str
    signature: str | None
    slot: int | None
    commitment: str | None
    received_at: datetime
    payload_json: str
    payload_sha256: str
    status: str
    safe_error_code: str | None
    safe_error_message: str | None
    idl_revision: str | None
    created_at: datetime


def canonical_payload_json(payload: dict[str, Any]) -> str:
    """Serialize raw JSON exactly as the T003 ProviderEnvelope contract does."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def event_key_for(envelope: ProviderEnvelope) -> str:
    """Return the stable 64-character representation of provider identity.

    The digest is a compatibility representation for the existing String(64) column;
    the canonical identity input remains exactly provider/transport/notification_id.
    """
    identity = "\x1f".join((envelope.provider, envelope.transport.value, envelope.notification_id))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


SessionSource = Session | sessionmaker[Session] | Callable[[], Session]


class RawJournal:
    """Insert-only repository for validated ProviderEnvelope values."""

    def __init__(self, session_source: SessionSource) -> None:
        self._session_source = session_source

    def _session(self) -> tuple[Session, bool]:
        if isinstance(self._session_source, Session):
            return self._session_source, False
        return self._session_source(), True

    @staticmethod
    def _record(row: Any) -> RawEventRecord:
        values = row._mapping if hasattr(row, "_mapping") else row
        record_values = dict(values)
        for field in ("received_at", "created_at"):
            timestamp = record_values[field]
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                record_values[field] = timestamp.replace(tzinfo=UTC)
            else:
                record_values[field] = timestamp.astimezone(UTC)
        return RawEventRecord(**record_values)

    def insert(
        self,
        envelope: ProviderEnvelope,
        *,
        before_commit: Callable[[Session, RawEventRecord], None] | None = None,
    ) -> RawEventRecord:
        """Persist an envelope and optional dependent state in the same transaction.

        The callback also runs for duplicates, using the original immutable record.
        A callback failure rolls back all changes made by this operation.
        """
        payload_json = canonical_payload_json(envelope.raw_payload)
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        if envelope.raw_payload_sha256 != payload_sha256:
            raise RawJournalHashError("raw payload SHA-256 does not match raw payload")

        event_key = event_key_for(envelope)
        session, owns_session = self._session()
        try:
            existing = session.execute(
                select(raw_events).where(raw_events.c.event_key == event_key)
            ).first()
            if existing is not None:
                if existing._mapping["payload_sha256"] != payload_sha256:
                    raise RawJournalConflictError(
                        "raw event identity already exists with a different payload hash"
                    )
                record = self._record(existing)
                if before_commit is not None:
                    before_commit(session, record)
                session.commit()
                return record

            now = datetime.now(UTC)
            values = {
                "event_key": event_key,
                "provider": envelope.provider,
                "transport": envelope.transport.value,
                "notification_id": envelope.notification_id,
                "signature": envelope.signature,
                "slot": envelope.slot,
                "commitment": envelope.commitment.value,
                "received_at": envelope.received_at,
                "payload_json": payload_json,
                "payload_sha256": payload_sha256,
                "status": "new",
                "safe_error_code": None,
                "safe_error_message": None,
                "idl_revision": envelope.idl_revision,
                "created_at": now,
            }
            try:
                session.execute(insert(raw_events).values(values))
            except IntegrityError:
                session.rollback()
                raced = session.execute(
                    select(raw_events).where(raw_events.c.event_key == event_key)
                ).first()
                if raced is None:
                    raise
                if raced._mapping["payload_sha256"] != payload_sha256:
                    raise RawJournalConflictError(
                        "raw event identity already exists with a different payload hash"
                    ) from None
                record = self._record(raced)
                if before_commit is not None:
                    before_commit(session, record)
                session.commit()
                return record

            persisted = session.execute(
                select(raw_events).where(raw_events.c.event_key == event_key)
            ).first()
            if persisted is None:
                raise RawJournalError("raw event insert did not produce a persisted row")
            record = self._record(persisted)
            if before_commit is not None:
                before_commit(session, record)
            session.commit()
            return record
        except Exception:
            if session.in_transaction():
                session.rollback()
            raise
        finally:
            if owns_session:
                session.close()


def insert_raw_event(session: Session, envelope: ProviderEnvelope) -> RawEventRecord:
    """Convenience function for callers that prefer a function repository boundary."""
    return RawJournal(session).insert(envelope)
