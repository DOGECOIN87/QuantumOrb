"""Persist decoder lifecycle state separately from immutable raw payload data."""

from __future__ import annotations

import re
from collections.abc import Callable

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import raw_events
from app.domain.enums import DecodeStatus

SessionSource = Session | sessionmaker[Session] | Callable[[], Session]


class DecodeOutcomeError(ValueError):
    """Base class for safe decode-state persistence errors."""


class DecodeOutcomeNotFoundError(DecodeOutcomeError):
    """The raw event does not exist in the immutable journal."""


class DecodeOutcomeConflictError(DecodeOutcomeError):
    """A terminal raw-event status cannot be replaced by this outcome."""


class DecodeOutcomeRepository:
    """Record decoder lifecycle fields while leaving the stored payload unchanged."""

    _REPROCESSABLE = {"new", DecodeStatus.RAW_ONLY.value, DecodeStatus.FAILED.value}

    def __init__(self, session_source: SessionSource) -> None:
        self._session_source = session_source

    def _session(self) -> tuple[Session, bool]:
        if isinstance(self._session_source, Session):
            return self._session_source, False
        return self._session_source(), True

    @staticmethod
    def _validate_outcome(
        status: DecodeStatus,
        safe_error_code: str | None,
        safe_error_message: str | None,
    ) -> None:
        if status == DecodeStatus.DECODED:
            if safe_error_code is not None or safe_error_message is not None:
                raise DecodeOutcomeError("decoded outcome cannot include an error")
            return
        if (
            safe_error_code is None
            or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", safe_error_code) is None
            or safe_error_message is None
            or not safe_error_message
            or len(safe_error_message) > 256
            or "\n" in safe_error_message
            or "\r" in safe_error_message
        ):
            raise DecodeOutcomeError("non-decoded outcome requires a safe diagnostic")

    def record_outcome(
        self,
        *,
        raw_event_id: int,
        status: DecodeStatus,
        safe_error_code: str | None,
        safe_error_message: str | None,
    ) -> None:
        """Update only lifecycle/error columns; identical outcomes are idempotent."""
        self._validate_outcome(status, safe_error_code, safe_error_message)
        session, owns_session = self._session()
        try:
            existing = session.execute(
                select(
                    raw_events.c.status,
                    raw_events.c.safe_error_code,
                    raw_events.c.safe_error_message,
                ).where(raw_events.c.id == raw_event_id)
            ).first()
            if existing is None:
                raise DecodeOutcomeNotFoundError("raw event does not exist")
            stored = existing._mapping
            target = (status.value, safe_error_code, safe_error_message)
            current = (
                stored["status"],
                stored["safe_error_code"],
                stored["safe_error_message"],
            )
            if current == target:
                session.commit()
                return
            if stored["status"] not in self._REPROCESSABLE:
                raise DecodeOutcomeConflictError("raw event decode state cannot be replaced")

            session.execute(
                update(raw_events)
                .where(raw_events.c.id == raw_event_id)
                .values(
                    status=status.value,
                    safe_error_code=safe_error_code,
                    safe_error_message=safe_error_message,
                )
            )
            session.commit()
        except Exception:
            if session.in_transaction():
                session.rollback()
            raise
        finally:
            if owns_session:
                session.close()
