"""Atomic raw journaling and durable per-program stream recovery anchors.

An observation cursor is a high-water mark, not proof that earlier slots are
complete. Recovery freezes its own anchor so new stream observations cannot move
the boundary while a bounded backfill is still running.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, insert, select, update
from sqlalchemy.orm import Session

from app.db.models import cursors
from app.db.raw_journal import RawEventRecord, RawJournal, SessionSource
from app.domain.models import ProviderEnvelope, _signature
from app.normalization import _validate_public_key

_TRANSPORTS = {"parsed_streams", "logs_subscribe"}
_TIMES = {"received_at", "pending_since", "anchor_received_at"}


class StreamCursorError(ValueError):
    """A stream cursor or its persisted representation is invalid."""


class StreamCursorConflictError(StreamCursorError):
    """A concurrent update requires retrying the complete transaction."""


@dataclass(frozen=True)
class StreamCursor:
    """Durable observation high-water mark and independent recovery boundary."""

    transport: str
    program_id: str
    slot: int | None
    signature: str | None
    received_at: datetime
    raw_event_id: int | None
    pending_since: datetime | None = None
    anchor_slot: int | None = None
    anchor_signature: str | None = None
    anchor_received_at: datetime | None = None
    data_gap: bool = False
    gap_reason: str | None = None


def _time(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise StreamCursorError("stream cursor timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _identity(transport: str, program_id: str) -> str:
    if transport not in _TRANSPORTS:
        raise StreamCursorError("stream cursor transport is unsupported")
    try:
        _validate_public_key(program_id)
    except (TypeError, ValueError):
        raise StreamCursorError("stream cursor program is invalid") from None
    return f"stream:mainnet-beta:{transport}:{program_id}"


def _reason(value: str | None) -> None:
    if value is not None and (
        not isinstance(value, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value) is None
    ):
        raise StreamCursorError("stream cursor gap reason must be a safe identifier")


def _position(slot: int | None, signature: str | None) -> None:
    if (slot is None) != (signature is None):
        raise StreamCursorError("stream cursor position is incomplete")
    if slot is not None and (type(slot) is not int or slot < 0):
        raise StreamCursorError("stream cursor slot is invalid")
    if signature is not None:
        try:
            _signature(signature)
        except (TypeError, ValueError):
            raise StreamCursorError("stream cursor signature is invalid") from None


def _validate(cursor: StreamCursor) -> None:
    _identity(cursor.transport, cursor.program_id)
    _position(cursor.slot, cursor.signature)
    _position(cursor.anchor_slot, cursor.anchor_signature)
    _time(cursor.received_at)
    if (cursor.slot is None) != (cursor.raw_event_id is None) or (
        cursor.raw_event_id is not None
        and (type(cursor.raw_event_id) is not int or cursor.raw_event_id <= 0)
    ):
        raise StreamCursorError("stream cursor raw reference is invalid")
    if type(cursor.data_gap) is not bool or cursor.data_gap != (cursor.gap_reason is not None):
        raise StreamCursorError("stream cursor gap state is inconsistent")
    _reason(cursor.gap_reason)
    if cursor.pending_since is None:
        if any(
            value is not None
            for value in (
                cursor.anchor_slot,
                cursor.anchor_signature,
                cursor.anchor_received_at,
            )
        ):
            raise StreamCursorError("stream cursor recovery state is inconsistent")
    else:
        _time(cursor.pending_since)
        _time(cursor.anchor_received_at)


def _json(cursor: StreamCursor) -> str:
    _validate(cursor)
    values = asdict(cursor)
    for key in _TIMES:
        if values[key] is not None:
            values[key] = _time(values[key]).isoformat()
    return json.dumps({"version": 1, **values}, sort_keys=True, separators=(",", ":"))


def _pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in values:
        if key in result:
            raise StreamCursorError("stream cursor JSON is invalid")
        result[key] = value
    return result


def _record(row: Any, transport: str, program_id: str) -> StreamCursor:
    try:
        stored = row._mapping
        encoded = stored["cursor_json"]
        if not isinstance(encoded, str) or len(encoded) > 4096:
            raise ValueError
        values = json.loads(encoded, object_pairs_hook=_pairs)
        if not isinstance(values, dict):
            raise ValueError
        version = values.pop("version", None)
        if type(version) is not int or version != 1:
            raise ValueError
        if set(values) != set(StreamCursor.__dataclass_fields__):
            raise ValueError
        for key in _TIMES:
            value = values[key]
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError
                values[key] = _time(datetime.fromisoformat(value))
        cursor = StreamCursor(**values)
        _validate(cursor)
        if (
            cursor.transport != transport
            or cursor.program_id != program_id
            or stored["name"] != _identity(transport, program_id)
            or stored["last_received_slot"] != cursor.slot
            or stored["last_received_signature"] != cursor.signature
        ):
            raise ValueError
        return cursor
    except (ValueError, TypeError, KeyError, OverflowError):
        raise StreamCursorError("persisted stream cursor is invalid") from None


class StreamCursorRepository:
    """Persist only cursor facts justified by retained notification envelopes."""

    def __init__(self, session_source: SessionSource) -> None:
        self._journal = RawJournal(session_source)

    @staticmethod
    def _load_row(session: Session, name: str) -> Any:
        return session.execute(
            select(cursors).where(cursors.c.name == name).with_for_update()
        ).first()

    @staticmethod
    def _write(
        session: Session,
        cursor: StreamCursor,
        row: Any,
        *,
        now: datetime,
        success: bool = False,
    ) -> None:
        name = _identity(cursor.transport, cursor.program_id)
        values: dict[str, Any] = {
            "cursor_json": _json(cursor),
            "last_received_slot": cursor.slot,
            "last_received_signature": cursor.signature,
            "updated_at": _time(now),
        }
        if success:
            values["last_success_at"] = _time(now)
        if row is None:
            session.execute(insert(cursors).values(name=name, **values))
            return
        result = session.execute(
            update(cursors)
            .where(cursors.c.name == name, cursors.c.cursor_json == row._mapping["cursor_json"])
            .values(**values)
        )
        if cast(CursorResult[Any], result).rowcount != 1:
            raise StreamCursorConflictError("stream cursor changed concurrently")

    def load(self, transport: str, program_id: str) -> StreamCursor | None:
        """Read a strictly validated durable cursor without creating one."""
        name = _identity(transport, program_id)
        session, owns_session = self._journal._session()
        try:
            row = self._load_row(session, name)
            return None if row is None else _record(row, transport, program_id)
        finally:
            if owns_session:
                session.close()

    def journal(self, envelope: ProviderEnvelope, program_id: str) -> RawEventRecord:
        """Commit the immutable notification and observation cursor atomically."""
        transport = envelope.transport.value
        name = _identity(transport, program_id)
        _position(envelope.slot, envelope.signature)

        def observe(session: Session, record: RawEventRecord) -> None:
            if record.slot is None or record.signature is None:
                raise StreamCursorError("raw notification position is incomplete")
            row = self._load_row(session, name)
            current = None if row is None else _record(row, transport, program_id)
            if current is not None and current.slot is not None and record.slot <= current.slot:
                return
            if current is None:
                cursor = StreamCursor(
                    transport,
                    program_id,
                    record.slot,
                    record.signature,
                    record.received_at,
                    record.id,
                )
            else:
                cursor = replace(
                    current,
                    slot=record.slot,
                    signature=record.signature,
                    received_at=record.received_at,
                    raw_event_id=record.id,
                )
            self._write(session, cursor, row, now=record.received_at)

        return self._journal.insert(envelope, before_commit=observe)

    def begin_recovery(self, transport: str, program_id: str, now: datetime) -> StreamCursor:
        """Freeze the pre-reconnect observation boundary once, including on restart."""
        name = _identity(transport, program_id)
        now = _time(now)
        session, owns_session = self._journal._session()
        try:
            row = self._load_row(session, name)
            current = (
                StreamCursor(transport, program_id, None, None, now, None)
                if row is None
                else _record(row, transport, program_id)
            )
            if current.pending_since is not None:
                session.commit()
                return current
            cursor = replace(
                current,
                pending_since=now,
                anchor_slot=current.slot,
                anchor_signature=current.signature,
                anchor_received_at=current.received_at,
            )
            self._write(session, cursor, row, now=now)
            session.commit()
            return cursor
        except Exception:
            session.rollback()
            raise
        finally:
            if owns_session:
                session.close()

    def finish_recovery(
        self,
        transport: str,
        program_id: str,
        *,
        now: datetime,
        gap_reason: str | None,
    ) -> StreamCursor:
        """Complete this attempt while preserving any prior unfilled coverage gap."""
        name = _identity(transport, program_id)
        now = _time(now)
        _reason(gap_reason)
        session, owns_session = self._journal._session()
        try:
            row = self._load_row(session, name)
            if row is None:
                raise StreamCursorError("stream cursor does not exist")
            current = _record(row, transport, program_id)
            cursor = replace(
                current,
                pending_since=None,
                anchor_slot=None,
                anchor_signature=None,
                anchor_received_at=None,
                data_gap=current.data_gap or gap_reason is not None,
                gap_reason=current.gap_reason or gap_reason,
            )
            self._write(session, cursor, row, now=now, success=gap_reason is None)
            session.commit()
            return cursor
        except Exception:
            session.rollback()
            raise
        finally:
            if owns_session:
                session.close()

    def mark_gap(
        self,
        transport: str,
        program_id: str,
        *,
        now: datetime,
        gap_reason: str,
    ) -> StreamCursor:
        """Record uncertain coverage without releasing an unfinished recovery anchor."""
        name = _identity(transport, program_id)
        now = _time(now)
        _reason(gap_reason)
        if gap_reason is None:
            raise StreamCursorError("stream cursor gap reason is required")
        session, owns_session = self._journal._session()
        try:
            row = self._load_row(session, name)
            if row is None:
                raise StreamCursorError("stream cursor does not exist")
            current = _record(row, transport, program_id)
            cursor = replace(current, data_gap=True, gap_reason=current.gap_reason or gap_reason)
            self._write(session, cursor, row, now=now)
            session.commit()
            return cursor
        except Exception:
            session.rollback()
            raise
        finally:
            if owns_session:
                session.close()
