"""Immutable journal for read-only RPC evidence, including empty and null results."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from app.db.models import raw_events
from app.db.raw_journal import RawEventRecord, RawJournal, SessionSource
from app.domain.enums import Commitment, Transport
from app.domain.models import _signature


class RpcJournalError(ValueError):
    """A fixed, safe diagnostic for rejected RPC journal evidence."""

    _CODES = {
        "rpc_response_unjournalable",
        "rpc_journal_conflict",
        "rpc_journal_invalid_config",
        "rpc_journal_invalid_context",
    }

    def __init__(self, code: str) -> None:
        self.code = (
            code if isinstance(code, str) and code in self._CODES else "rpc_journal_invalid_context"
        )
        super().__init__("RPC response journaling failed")


class RpcResponseJournal:
    """Retain RPC results with their exact request context for replay and audit.

    Listing results have no transaction signature. Nullable raw columns express
    that honestly. The default identity matches the T020 recovery journal.
    """

    def __init__(
        self,
        session_source: SessionSource,
        transport: str,
        revision: str,
        *,
        notification_prefix: str = "recovery-rpc",
        commitment: str = "confirmed",
    ) -> None:
        if (
            not isinstance(transport, str)
            or transport not in {item.value for item in Transport}
            or not isinstance(commitment, str)
            or commitment not in {item.value for item in Commitment}
            or not isinstance(revision, str)
            or not revision
            or not isinstance(notification_prefix, str)
            or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", notification_prefix) is None
        ):
            raise RpcJournalError("rpc_journal_invalid_config")
        self._source = session_source
        self._transport = transport
        self._revision = revision
        self._prefix = notification_prefix
        self._commitment = commitment

    def insert(
        self,
        *,
        method: str,
        program_id: str,
        params: list[Any],
        response: Any,
        received_at: datetime,
        signature: str | None = None,
        slot: int | None = None,
        bare: bool = False,
    ) -> RawEventRecord:
        """Commit raw evidence before decoding, preserving the first observation."""
        if (
            not isinstance(method, str)
            or not method
            or not isinstance(program_id, str)
            or not program_id
            or not isinstance(params, list)
            or not isinstance(received_at, datetime)
            or received_at.tzinfo is None
            or received_at.utcoffset() is None
            or (slot is not None and (type(slot) is not int or slot < 0))
            or type(bare) is not bool
        ):
            raise RpcJournalError("rpc_journal_invalid_context")
        if signature is not None:
            try:
                _signature(signature)
            except (TypeError, ValueError):
                raise RpcJournalError("rpc_journal_invalid_context") from None
        received_at = received_at.astimezone(UTC)
        context = {"method": method, "program_id": program_id, "params": params}
        retained = response if bare else {"request": context, "response": response}
        try:
            payload_json = json.dumps(
                retained, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
            identity_json = json.dumps(
                {"transport": self._transport, "request": context, "payload": retained},
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError, RecursionError):
            raise RpcJournalError("rpc_response_unjournalable") from None
        notification_id = self._prefix + ":" + hashlib.sha256(identity_json.encode()).hexdigest()
        event_key = hashlib.sha256(
            "\x1f".join(("helius", self._transport, notification_id)).encode()
        ).hexdigest()
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
        journal = RawJournal(self._source)
        session, owned = journal._session()
        try:
            existing = session.execute(
                select(raw_events).where(raw_events.c.event_key == event_key)
            ).first()
            if existing is None:
                try:
                    session.execute(
                        insert(raw_events).values(
                            event_key=event_key,
                            provider="helius",
                            transport=self._transport,
                            notification_id=notification_id,
                            signature=signature,
                            slot=slot,
                            commitment=self._commitment,
                            received_at=received_at,
                            payload_json=payload_json,
                            payload_sha256=payload_hash,
                            status="new",
                            safe_error_code=None,
                            safe_error_message=None,
                            idl_revision=self._revision,
                            created_at=received_at,
                        )
                    )
                except IntegrityError:
                    session.rollback()
                existing = session.execute(
                    select(raw_events).where(raw_events.c.event_key == event_key)
                ).first()
            if (
                existing is None
                or existing._mapping["payload_sha256"] != payload_hash
                or existing._mapping["commitment"] != self._commitment
                or existing._mapping["signature"] != signature
                or existing._mapping["slot"] != slot
            ):
                raise RpcJournalError("rpc_journal_conflict")
            record = journal._record(existing)
            session.commit()
            return record
        except BaseException:
            session.rollback()
            raise
        finally:
            if owned:
                session.close()
