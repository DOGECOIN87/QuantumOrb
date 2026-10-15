"""Finalized signature lookup and evidence-preserving Scout reconciliation."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol

from pydantic import Field, StrictBool, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.canonical_events import (
    CanonicalEventRepository,
    EventReconciliation,
    ReconciliationOutcome,
    ReconciliationRecord,
    ScoutEventRecord,
    _scout_record,
    canonical_events_match,
)
from app.db.decode_outcomes import DecodeOutcomeConflictError, DecodeOutcomeRepository
from app.db.lifecycles import LifecycleContextUnavailable, LifecycleRepository
from app.db.models import scout_events, token_lifecycles
from app.db.raw_journal import RawEventRecord, RawJournal, RawJournalError
from app.domain.enums import Commitment, DecodeStatus, ProgramName
from app.domain.keys import canonical_event_key_for
from app.domain.models import CanonicalSolanaEvent, DomainModel, ProviderEnvelope, _signature

SessionFactory = sessionmaker[Session] | Callable[[], Session]


class FinalityError(ValueError):
    """Safe finality input error, without provider response or credential text."""


class FinalizedDecodeResult(DomainModel):
    """Explicit transaction-wide completeness, never inferred from an empty list."""

    status: DecodeStatus
    complete: StrictBool
    events: list[CanonicalSolanaEvent] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_decode_shape(self) -> FinalizedDecodeResult:
        if self.status != DecodeStatus.DECODED and (self.events or self.complete):
            raise ValueError("unsuccessful decode cannot claim complete canonical events")
        return self


class FinalityLookup(Protocol):
    """Return the whole finalized observation, or None when unavailable.

    None does not prove that a confirmed transaction disappeared. Adapters own
    bounded network retry behavior; the reconciler makes one lookup per call.
    """

    async def lookup_signature(
        self, signature: str, *, commitment: Commitment
    ) -> ProviderEnvelope | None: ...


class FinalityDecoder(Protocol):
    """Decode only a durably journaled finalized transaction through its adapter."""

    def decode(self, raw: RawEventRecord) -> FinalizedDecodeResult: ...


class FinalityReconciler:
    """Reconcile all confirmed events for one signature, without starting ingestion."""

    def __init__(
        self, session_factory: SessionFactory, lookup: FinalityLookup, decoder: FinalityDecoder
    ) -> None:
        self._sessions = session_factory
        self._lookup = lookup
        self._decoder = decoder
        self._journal = RawJournal(session_factory)
        self._outcomes = DecodeOutcomeRepository(session_factory)

    async def reconcile_signature(self, signature: str) -> list[ReconciliationRecord]:
        try:
            _signature(signature)
        except (ValueError, TypeError):
            raise FinalityError("finality lookup requires a valid Solana signature") from None
        if not CanonicalEventRepository(self._sessions).confirmed_for_signature(signature):
            return []

        raw: RawEventRecord | None = None
        events: list[CanonicalSolanaEvent] | None = None
        try:
            envelope = await self._lookup.lookup_signature(
                signature, commitment=Commitment.FINALIZED
            )
        except Exception:
            # Provider exceptions may contain credentials. Persist only a
            # fixed diagnostic, never exception or validation text.
            return self._apply(signature, None, None)
        if envelope is None:
            return self._apply(signature, None, None)
        try:
            if not isinstance(envelope, ProviderEnvelope):
                raise ValueError("lookup envelope has an invalid type")
            envelope = ProviderEnvelope.model_validate(envelope.model_dump(warnings=False))
            raw = self._journal.insert(envelope)
        except (ValidationError, RawJournalError, ValueError):
            return self._apply(signature, None, None)

        status = DecodeStatus.FAILED
        if (
            envelope.signature == signature
            and envelope.commitment == Commitment.FINALIZED
            and envelope.transaction_error is None
            and raw.signature == signature
            and raw.commitment == Commitment.FINALIZED.value
            and raw.slot == envelope.slot
        ):
            try:
                decoded = self._decoder.decode(raw)
                if not isinstance(decoded, FinalizedDecodeResult):
                    raise ValueError("invalid decode result type")
                decoded = FinalizedDecodeResult.model_validate(decoded.model_dump(warnings=False))
                if decoded.status == DecodeStatus.DECODED and decoded.complete:
                    events = self._validated_events(signature, raw, decoded.events)
                    status = DecodeStatus.DECODED
                elif decoded.status != DecodeStatus.FAILED:
                    status = DecodeStatus.RAW_ONLY
            except Exception:
                events = None
        try:
            self._outcomes.record_outcome(
                raw_event_id=raw.id,
                status=status,
                safe_error_code=None
                if status == DecodeStatus.DECODED
                else "finality_decode_failed",
                safe_error_message=None
                if status == DecodeStatus.DECODED
                else "finalized lookup could not be decoded completely",
            )
        except DecodeOutcomeConflictError:
            # A previously decoded raw notification keeps its terminal status.
            # A later unsuccessful attempt is captured in the audit records.
            if status == DecodeStatus.DECODED:
                raise
        return self._apply(signature, raw, events)

    @staticmethod
    def _validated_events(
        signature: str, raw: RawEventRecord, events: Sequence[CanonicalSolanaEvent]
    ) -> list[CanonicalSolanaEvent]:
        identities: set[str] = set()
        for event in events:
            if (
                event.event_key != canonical_event_key_for(event)
                or event.event_key in identities
                or event.signature != signature
                or event.slot != raw.slot
                or event.raw_event_id != raw.id
                or event.commitment != Commitment.FINALIZED
                or event.decode_status != DecodeStatus.DECODED
                or event.program not in {ProgramName.PUMP, ProgramName.PUMP_AMM}
            ):
                raise FinalityError("finalized decoder metadata is inconsistent")
            identities.add(event.event_key)
        return list(events)

    def _apply(
        self,
        signature: str,
        raw: RawEventRecord | None,
        events: Sequence[CanonicalSolanaEvent] | None,
    ) -> list[ReconciliationRecord]:
        with self._sessions() as session, session.begin():
            LifecycleRepository._begin_sqlite_transaction(session)
            repository = CanonicalEventRepository(session)
            originals = repository.confirmed_for_signature(signature)
            by_key = {event.event_key: event for event in events or []}
            comparisons: list[EventReconciliation] = []
            for original in originals:
                counterpart = by_key.get(original.event_key)
                status: ReconciliationOutcome
                if events is None:
                    status = "failed"
                elif counterpart is None:
                    status = "missing"
                elif canonical_events_match(original, counterpart):
                    status = "matched"
                else:
                    status = "changed"
                comparisons.append(
                    EventReconciliation(
                        event_key=original.event_key,
                        status=status,
                        finalized_raw_event_id=raw.id if raw else None,
                        finalized_slot=raw.slot if raw else None,
                    )
                )
            records = repository.apply_reconciliations(comparisons)
            self._refresh_lifecycles(session, originals)
            return records

    @staticmethod
    def _refresh_lifecycles(session: Session, originals: Sequence[ScoutEventRecord]) -> None:
        # Refresh each affected token once, including a previously materialized
        # pool whose association may now be disputed. Do not invent a mint for
        # unresolved pool trades.
        affected = {event.mint for event in originals if event.mint is not None}
        pools = {event.pool for event in originals if event.pool is not None}
        if pools:
            affected.update(
                session.scalars(
                    select(token_lifecycles.c.mint).where(token_lifecycles.c.pool.in_(pools))
                )
            )
        lifecycle = LifecycleRepository(session)
        for mint in sorted(affected):
            anchor = session.execute(
                select(scout_events)
                .where(scout_events.c.mint == mint)
                .order_by(scout_events.c.id)
                .limit(1)
            ).first()
            if anchor is not None:
                lifecycle.apply(_scout_record(anchor))
        for original in originals:
            if original.mint is None:
                try:
                    lifecycle.apply(original)
                except LifecycleContextUnavailable:
                    pass
