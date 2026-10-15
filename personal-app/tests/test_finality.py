"""Offline finalized lookup, durable evidence, correction, and lifecycle tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.canonical_events import CanonicalEventRepository, ScoutEventRecord
from app.db.lifecycles import LifecycleRepository
from app.db.models import raw_events, reconciliation_records, scout_events, token_lifecycles
from app.db.raw_journal import RawEventRecord, RawJournal
from app.db.session import create_database_engine, create_session_factory
from app.domain.enums import Commitment, DecodeStatus
from app.domain.keys import canonical_event_key
from app.domain.models import CanonicalSolanaEvent, ProviderEnvelope
from app.finality import FinalityError, FinalityReconciler, FinalizedDecodeResult

MINT = "1" * 32
OTHER_MINT = "1" * 31 + "2"
CURVE = "1" * 31 + "3"
CREATOR = "1" * 31 + "4"
USER = "1" * 31 + "5"
SIGNATURE = "1" * 64
OTHER_SIGNATURE = "1" * 63 + "2"
NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)
SECRET = "credential-that-must-not-appear-in-safe-errors"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    database = create_database_engine(f"sqlite:///{tmp_path / 'finality.db'}")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


def envelope(
    *,
    notification_id: str = "finalized-fixture",
    signature: str = SIGNATURE,
    slot: int = 42,
    commitment: Commitment = Commitment.FINALIZED,
) -> ProviderEnvelope:
    payload = {"fixture": notification_id, "signature": signature, "slot": slot}
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return ProviderEnvelope(
        transport="fixture",
        notification_id=notification_id,
        received_at=NOW + timedelta(minutes=1),
        commitment=commitment,
        slot=slot,
        signature=signature,
        raw_payload=payload,
        raw_payload_sha256=hashlib.sha256(payload_json.encode()).hexdigest(),
    )


def canonical(
    raw_id: int,
    *,
    event_index: int = 0,
    event_type: str = "token_created",
    mint: str | None = MINT,
    signature: str = SIGNATURE,
) -> CanonicalSolanaEvent:
    name = {
        "token_created": "CreateEvent",
        "curve_buy": "TradeEvent",
        "curve_complete": "CompleteEvent",
    }[event_type]
    return CanonicalSolanaEvent(
        event_key=canonical_event_key("helius", signature, "pump-fixture", name, event_index),
        signature=signature,
        slot=42,
        block_time=NOW,
        observed_at=NOW,
        commitment="confirmed",
        program="pump",
        program_id="pump-fixture",
        event_name=name,
        event_type=event_type,
        mint=mint,
        user=USER,
        creator=CREATOR,
        bonding_curve=CURVE,
        transaction_index=0,
        instruction_index=0,
        event_index=event_index,
        payload={"name": "fixture", "amount": "9007199254740993"},
        decode_status="decoded",
        finality_status="provisional",
        reconciliation_status="pending",
        derivation_version="fixture-v1",
        raw_event_id=raw_id,
    )


def observe(
    factory: sessionmaker[Session],
    *,
    event_types: tuple[str, ...] = ("token_created",),
    mints: tuple[str | None, ...] | None = None,
) -> tuple[list[CanonicalSolanaEvent], list[ScoutEventRecord]]:
    confirmed = RawJournal(factory).insert(
        envelope(notification_id="confirmed-fixture", commitment=Commitment.CONFIRMED)
    )
    events = [
        canonical(
            confirmed.id,
            event_index=index,
            event_type=event_type,
            mint=mints[index] if mints is not None else MINT,
        )
        for index, event_type in enumerate(event_types)
    ]
    stored = [CanonicalEventRepository(factory).insert_if_absent(event) for event in events]
    for record in stored:
        LifecycleRepository(factory).apply(record)
    return events, stored


class LookupSpy:
    def __init__(self, result: ProviderEnvelope | None, *, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[str, Commitment]] = []

    async def lookup_signature(
        self, signature: str, *, commitment: Commitment
    ) -> ProviderEnvelope | None:
        self.calls.append((signature, commitment))
        if self.error is not None:
            raise self.error
        return self.result


class DecoderSpy:
    def __init__(
        self,
        events: list[CanonicalSolanaEvent],
        *,
        status: DecodeStatus = DecodeStatus.DECODED,
        complete: bool = True,
        mutate: (
            Callable[[CanonicalSolanaEvent, RawEventRecord], CanonicalSolanaEvent] | None
        ) = None,
        before_decode: Callable[[RawEventRecord], None] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.events = events
        self.status = status
        self.complete = complete
        self.mutate = mutate
        self.before_decode = before_decode
        self.error = error
        self.calls: list[RawEventRecord] = []

    def decode(self, raw: RawEventRecord) -> FinalizedDecodeResult:
        self.calls.append(raw)
        if self.before_decode is not None:
            self.before_decode(raw)
        if self.error is not None:
            raise self.error
        finalized = []
        for event in self.events:
            result = event.model_copy(
                update={
                    "commitment": Commitment.FINALIZED,
                    "finality_status": "finalized",
                    "reconciliation_status": "not_required",
                    "raw_event_id": raw.id,
                    "slot": raw.slot,
                    "observed_at": NOW + timedelta(minutes=1),
                    "derivation_version": "fixture-v2",
                }
            )
            finalized.append(self.mutate(result, raw) if self.mutate is not None else result)
        return FinalizedDecodeResult(status=self.status, complete=self.complete, events=finalized)


def rows(factory: sessionmaker[Session], table: Any) -> list[dict[str, Any]]:
    with factory() as session:
        return [dict(row._mapping) for row in session.execute(select(table))]


def reconcile(factory: sessionmaker[Session], lookup: LookupSpy, decoder: DecoderSpy) -> list[Any]:
    service = FinalityReconciler(factory, lookup, decoder)
    return asyncio.run(service.reconcile_signature(SIGNATURE))


def test_matched_finality_retains_original_evidence_and_refreshes_lifecycle(
    factory: sessionmaker[Session],
) -> None:
    events, stored = observe(factory)
    initial = rows(factory, scout_events)[0]
    lookup = LookupSpy(envelope())
    decoder = DecoderSpy(events)

    result = reconcile(factory, lookup, decoder)

    assert lookup.calls == [(SIGNATURE, Commitment.FINALIZED)]
    assert len(result) == 1
    assert result[0].status == "matched"
    assert result[0].reason is None
    assert result[0].event_key == stored[0].event_key
    assert result[0].confirmed_payload_ref == stored[0].raw_event_id
    assert result[0].finalized_payload_ref == decoder.calls[0].id
    assert result[0].confirmed_slot == result[0].finalized_slot == 42
    assert result[0].resolved_at is not None
    updated = rows(factory, scout_events)[0]
    assert updated["finality_status"] == "finalized"
    assert updated["reconciliation_status"] == "matched"
    for field in initial.keys() - {"finality_status", "reconciliation_status"}:
        assert updated[field] == initial[field]
    lifecycle = rows(factory, token_lifecycles)[0]
    assert lifecycle["provisional"] is False
    assert lifecycle["data_gap"] is False
    assert len(rows(factory, raw_events)) == 2


@pytest.mark.parametrize(
    "change",
    [
        {"payload": {"name": "changed"}},
        {"block_time": NOW + timedelta(seconds=1)},
        {"program": "pump_amm"},
        {"event_type": "curve_buy"},
        {"mint": OTHER_MINT},
        {"creator": OTHER_MINT},
        {"user": OTHER_MINT},
        {"bonding_curve": OTHER_MINT},
        {"pool": OTHER_MINT},
        {"quote_mint": OTHER_MINT},
        {"transaction_index": 1},
        {"instruction_index": 1},
    ],
)
def test_changed_semantic_fields_preserve_original_projection_and_carry_uncertainty(
    factory: sessionmaker[Session], change: dict[str, Any]
) -> None:
    events, _ = observe(factory)
    original = rows(factory, scout_events)[0]
    decoder = DecoderSpy(events, mutate=lambda event, raw: event.model_copy(update=change))
    result = reconcile(factory, LookupSpy(envelope()), decoder)

    assert result[0].status == "changed"
    assert result[0].reason is not None
    current = rows(factory, scout_events)[0]
    assert current["finality_status"] == "reorged_or_changed"
    assert current["reconciliation_status"] == "changed"
    for field in original.keys() - {"finality_status", "reconciliation_status"}:
        assert current[field] == original[field]
    lifecycle = rows(factory, token_lifecycles)[0]
    assert lifecycle["state"] == "seen_create"
    assert lifecycle["provisional"] is True
    assert lifecycle["data_gap"] is True
    assert len(rows(factory, scout_events)) == 1


def test_changed_finalized_slot_is_recorded_without_rewriting_confirmed_slot(
    factory: sessionmaker[Session],
) -> None:
    events, _ = observe(factory)
    result = reconcile(factory, LookupSpy(envelope(slot=43)), DecoderSpy(events))
    assert result[0].status == "changed"
    assert result[0].confirmed_slot == 42
    assert result[0].finalized_slot == 43
    assert rows(factory, scout_events)[0]["slot"] == 42


@pytest.mark.parametrize("identity_part", ["program_id", "event_name", "event_index"])
def test_complete_finalized_decode_with_different_identity_marks_original_missing(
    factory: sessionmaker[Session], identity_part: str
) -> None:
    events, _ = observe(factory)

    def change_identity(event: CanonicalSolanaEvent, raw: RawEventRecord) -> CanonicalSolanaEvent:
        del raw
        changes = {identity_part: 1 if identity_part == "event_index" else "other"}
        changed = event.model_copy(update=changes)
        return changed.model_copy(
            update={
                "event_key": canonical_event_key(
                    "helius",
                    changed.signature,
                    changed.program_id,
                    changed.event_name,
                    changed.event_index,
                )
            }
        )

    result = reconcile(factory, LookupSpy(envelope()), DecoderSpy(events, mutate=change_identity))
    assert result[0].status == "missing"
    assert rows(factory, scout_events)[0]["reconciliation_status"] == "missing"
    assert rows(factory, token_lifecycles)[0]["data_gap"] is True
    assert len(rows(factory, scout_events)) == 1


def test_complete_successful_empty_decode_proves_missing_identity(
    factory: sessionmaker[Session],
) -> None:
    observe(factory)
    result = reconcile(factory, LookupSpy(envelope()), DecoderSpy([]))
    assert result[0].status == "missing"
    assert result[0].finalized_payload_ref is not None
    assert result[0].finalized_slot == 42
    assert rows(factory, token_lifecycles)[0]["provisional"] is True
    assert rows(factory, token_lifecycles)[0]["data_gap"] is True


@pytest.mark.parametrize(
    ("status", "complete"),
    [
        (DecodeStatus.DECODED, False),
        (DecodeStatus.RAW_ONLY, False),
        (DecodeStatus.RAW_ONLY, True),
        (DecodeStatus.FAILED, False),
        (DecodeStatus.FAILED, True),
    ],
)
def test_incomplete_or_unsuccessful_decode_cannot_prove_missing_or_matching_finality(
    factory: sessionmaker[Session], status: DecodeStatus, complete: bool
) -> None:
    events, _ = observe(factory)
    original = rows(factory, scout_events)[0]
    result = reconcile(
        factory, LookupSpy(envelope()), DecoderSpy(events, status=status, complete=complete)
    )
    assert result[0].status == "failed"
    assert result[0].reason is not None
    assert rows(factory, scout_events)[0] == original
    assert rows(factory, token_lifecycles)[0]["provisional"] is True
    assert rows(factory, token_lifecycles)[0]["data_gap"] is True
    assert len(rows(factory, raw_events)) == 2


def test_provider_unavailable_is_failure_without_decoder_or_fabricated_raw(
    factory: sessionmaker[Session],
) -> None:
    events, _ = observe(factory)
    original = rows(factory, scout_events)[0]
    decoder = DecoderSpy(events)
    result = reconcile(factory, LookupSpy(None), decoder)
    assert result[0].status == "failed"
    assert result[0].finalized_payload_ref is None
    assert result[0].finalized_slot is None
    assert decoder.calls == []
    assert rows(factory, scout_events)[0] == original
    assert len(rows(factory, raw_events)) == 1


def test_signature_without_confirmed_observations_does_not_call_provider(
    factory: sessionmaker[Session],
) -> None:
    lookup = LookupSpy(envelope())
    decoder = DecoderSpy([])
    assert reconcile(factory, lookup, decoder) == []
    assert lookup.calls == []
    assert decoder.calls == []
    assert rows(factory, raw_events) == []


@pytest.mark.parametrize("signature", ["", "not-a-solana-signature", "1" * 32])
def test_invalid_requested_signature_is_rejected_before_provider_or_database_writes(
    factory: sessionmaker[Session], signature: str
) -> None:
    lookup = LookupSpy(envelope())
    service = FinalityReconciler(factory, lookup, DecoderSpy([]))
    with pytest.raises(FinalityError, match="valid Solana signature"):
        asyncio.run(service.reconcile_signature(signature))
    assert lookup.calls == []
    assert rows(factory, raw_events) == []
    assert rows(factory, reconciliation_records) == []


def test_finalized_raw_is_committed_before_decoder_runs_in_another_session(
    factory: sessionmaker[Session],
) -> None:
    events, _ = observe(factory)

    def assert_durable(raw: RawEventRecord) -> None:
        with factory() as session:
            observed = (
                session.execute(select(raw_events).where(raw_events.c.id == raw.id)).one()._mapping
            )
            assert observed["payload_json"] == raw.payload_json
            assert observed["commitment"] == "finalized"
            assert session.scalar(select(func.count()).select_from(reconciliation_records)) == 0

    result = reconcile(
        factory, LookupSpy(envelope()), DecoderSpy(events, before_decode=assert_durable)
    )
    assert result[0].status == "matched"


@pytest.mark.parametrize(
    "bad_envelope",
    [
        envelope(signature=OTHER_SIGNATURE),
        envelope(commitment=Commitment.CONFIRMED),
    ],
)
def test_mismatched_provider_signature_or_commitment_is_safe_failure(
    factory: sessionmaker[Session], bad_envelope: ProviderEnvelope
) -> None:
    events, _ = observe(factory)
    decoder = DecoderSpy(events)
    original = rows(factory, scout_events)[0]
    result = reconcile(factory, LookupSpy(bad_envelope), decoder)
    assert result[0].status == "failed"
    assert decoder.calls == []
    assert rows(factory, scout_events)[0] == original


def test_failed_finalized_transaction_is_retained_without_decoding_or_claiming_missing(
    factory: sessionmaker[Session],
) -> None:
    events, _ = observe(factory)
    failed_transaction = envelope().model_copy(update={"transaction_error": {"failure": 1}})
    decoder = DecoderSpy(events)
    result = reconcile(factory, LookupSpy(failed_transaction), decoder)
    assert result[0].status == "failed"
    assert result[0].finalized_payload_ref is not None
    assert decoder.calls == []
    assert len(rows(factory, raw_events)) == 2
    assert rows(factory, scout_events)[0]["finality_status"] == "provisional"


def test_invalid_envelope_payload_hash_does_not_enter_raw_journal(
    factory: sessionmaker[Session],
) -> None:
    events, _ = observe(factory)
    invalid = envelope().model_copy(update={"raw_payload_sha256": "0" * 64})
    decoder = DecoderSpy(events)
    result = reconcile(factory, LookupSpy(invalid), decoder)
    assert result[0].status == "failed"
    assert result[0].finalized_payload_ref is None
    assert decoder.calls == []
    assert len(rows(factory, raw_events)) == 1


def test_reused_confirmed_raw_identity_cannot_certify_finalized_commitment(
    factory: sessionmaker[Session],
) -> None:
    events, _ = observe(factory)
    decoder = DecoderSpy(events)
    original = rows(factory, scout_events)[0]
    reused = envelope(notification_id="confirmed-fixture")
    result = reconcile(factory, LookupSpy(reused), decoder)
    assert result[0].status == "failed"
    assert decoder.calls == []
    assert len(rows(factory, raw_events)) == 1
    assert rows(factory, raw_events)[0]["commitment"] == "confirmed"
    assert rows(factory, scout_events)[0] == original
    assert rows(factory, token_lifecycles)[0]["data_gap"] is True


def test_conflicting_raw_identity_payload_is_rejected_without_replacing_evidence(
    factory: sessionmaker[Session],
) -> None:
    events, _ = observe(factory)
    original = rows(factory, raw_events)[0]
    decoder = DecoderSpy(events)
    conflict = envelope(notification_id="confirmed-fixture", slot=43)
    result = reconcile(factory, LookupSpy(conflict), decoder)
    assert result[0].status == "failed"
    assert result[0].finalized_payload_ref is None
    assert decoder.calls == []
    assert rows(factory, raw_events) == [original]
    assert rows(factory, scout_events)[0]["reconciliation_status"] == "pending"


@pytest.mark.parametrize(
    "changes",
    [
        {"commitment": Commitment.CONFIRMED},
        {"slot": 999},
        {"raw_event_id": 999},
        {"signature": OTHER_SIGNATURE},
        {"event_key": "0" * 64},
        {"decode_status": DecodeStatus.RAW_ONLY},
        {"program": "unknown"},
    ],
)
def test_decoded_events_require_finalized_signature_slot_provenance_and_identity(
    factory: sessionmaker[Session], changes: dict[str, Any]
) -> None:
    events, _ = observe(factory)
    decoder = DecoderSpy(events, mutate=lambda event, raw: event.model_copy(update=changes))
    original = rows(factory, scout_events)[0]
    result = reconcile(factory, LookupSpy(envelope()), decoder)
    assert result[0].status == "failed"
    assert rows(factory, scout_events)[0] == original
    assert rows(factory, token_lifecycles)[0]["data_gap"] is True


def test_duplicate_decoded_identity_invalidates_entire_comparison(
    factory: sessionmaker[Session],
) -> None:
    events, _ = observe(factory)
    original = rows(factory, scout_events)[0]
    result = reconcile(factory, LookupSpy(envelope()), DecoderSpy(events + events))
    assert result[0].status == "failed"
    assert rows(factory, scout_events)[0] == original


@pytest.mark.parametrize("where", ["provider", "decoder"])
def test_untrusted_exception_text_is_never_retained_or_logged(
    factory: sessionmaker[Session], caplog: pytest.LogCaptureFixture, where: str
) -> None:
    events, _ = observe(factory)
    lookup = LookupSpy(envelope(), error=RuntimeError(SECRET) if where == "provider" else None)
    decoder = DecoderSpy(events, error=RuntimeError(SECRET) if where == "decoder" else None)
    result = reconcile(factory, lookup, decoder)
    assert result[0].status == "failed"
    assert SECRET not in str(result)
    assert SECRET not in repr(rows(factory, reconciliation_records))
    assert SECRET not in repr(rows(factory, raw_events))
    assert SECRET not in caplog.text


def test_mixed_signature_results_are_compared_and_applied_together(
    factory: sessionmaker[Session],
) -> None:
    events, _ = observe(factory, event_types=("token_created", "curve_buy", "curve_complete"))

    def change_trade(event: CanonicalSolanaEvent, raw: RawEventRecord) -> CanonicalSolanaEvent:
        del raw
        if event.event_type.value == "curve_buy":
            return event.model_copy(update={"payload": {"name": "changed trade"}})
        return event

    lookup = LookupSpy(envelope())
    result = reconcile(factory, lookup, DecoderSpy(events[:2], mutate=change_trade))
    assert {record.event_key: record.status for record in result} == {
        events[0].event_key: "matched",
        events[1].event_key: "changed",
        events[2].event_key: "missing",
    }
    assert len(lookup.calls) == 1
    assert len(rows(factory, reconciliation_records)) == 3
    assert len(rows(factory, raw_events)) == 2
    assert len(rows(factory, scout_events)) == 3
    lifecycle = rows(factory, token_lifecycles)[0]
    assert lifecycle["state"] == "curve_complete"
    assert lifecycle["provisional"] is True
    assert lifecycle["data_gap"] is True


@pytest.mark.parametrize("outcome", ["matched", "changed", "missing", "failed"])
def test_repeated_identical_attempts_are_idempotent(
    factory: sessionmaker[Session], outcome: str
) -> None:
    events, _ = observe(factory)
    lookup = LookupSpy(None if outcome == "failed" else envelope())
    decoder = DecoderSpy(
        [] if outcome == "missing" else events,
        mutate=(
            (lambda event, raw: event.model_copy(update={"payload": {"changed": True}}))
            if outcome == "changed"
            else None
        ),
    )
    first = reconcile(factory, lookup, decoder)
    snapshots = {table.name: rows(factory, table) for table in (scout_events, token_lifecycles)}
    second = reconcile(factory, lookup, decoder)
    assert first == second
    assert first[0].status == outcome
    assert len(rows(factory, reconciliation_records)) == 1
    assert len(rows(factory, raw_events)) == (1 if outcome == "failed" else 2)
    for table in (scout_events, token_lifecycles):
        assert rows(factory, table) == snapshots[table.name]


def test_retry_after_lookup_failure_recovers_finality_and_repairs_lifecycle_gap(
    factory: sessionmaker[Session],
) -> None:
    events, _ = observe(factory)
    failed = reconcile(factory, LookupSpy(None), DecoderSpy(events))
    assert failed[0].status == "failed"
    assert rows(factory, token_lifecycles)[0]["data_gap"] is True
    matched = reconcile(factory, LookupSpy(envelope()), DecoderSpy(events))
    assert matched[0].status == "matched"
    assert len(rows(factory, reconciliation_records)) == 2
    lifecycle = rows(factory, token_lifecycles)[0]
    assert lifecycle["provisional"] is False
    assert lifecycle["data_gap"] is False


def test_incomplete_decode_can_retry_same_durable_raw_and_recover(
    factory: sessionmaker[Session],
) -> None:
    events, _ = observe(factory)
    first = reconcile(factory, LookupSpy(envelope()), DecoderSpy(events, complete=False))
    second = reconcile(factory, LookupSpy(envelope()), DecoderSpy(events))
    assert first[0].status == "failed"
    assert second[0].status == "matched"
    assert first[0].finalized_payload_ref == second[0].finalized_payload_ref
    assert len(rows(factory, reconciliation_records)) == 2
    assert len(rows(factory, raw_events)) == 2
    assert rows(factory, token_lifecycles)[0]["data_gap"] is False


def test_failure_after_matched_finality_retains_authoritative_flags_without_new_gap(
    factory: sessionmaker[Session],
) -> None:
    events, _ = observe(factory)
    assert reconcile(factory, LookupSpy(envelope()), DecoderSpy(events))[0].status == "matched"
    original = rows(factory, scout_events)[0]
    reconcile(factory, LookupSpy(None), DecoderSpy(events))
    assert rows(factory, scout_events)[0] == original
    lifecycle = rows(factory, token_lifecycles)[0]
    assert lifecycle["provisional"] is False
    assert lifecycle["data_gap"] is False


def test_failed_decoder_retry_preserves_terminal_raw_and_authoritative_finality(
    factory: sessionmaker[Session],
) -> None:
    events, _ = observe(factory)
    first = reconcile(factory, LookupSpy(envelope()), DecoderSpy(events))
    finalized_raw = rows(factory, raw_events)[-1]
    finalized_event = rows(factory, scout_events)[0]
    second = reconcile(
        factory, LookupSpy(envelope()), DecoderSpy(events, error=RuntimeError(SECRET))
    )
    assert first[0].status == "matched"
    assert second[0].status == "failed"
    assert second[0].finalized_payload_ref == first[0].finalized_payload_ref
    assert rows(factory, raw_events)[-1] == finalized_raw
    assert finalized_raw["status"] == "decoded"
    assert rows(factory, scout_events)[0] == finalized_event
    assert len(rows(factory, reconciliation_records)) == 2
    assert rows(factory, token_lifecycles)[0]["data_gap"] is False


def test_multi_event_corrections_rollback_when_second_lifecycle_write_fails(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    events, _ = observe(
        factory, event_types=("token_created", "token_created"), mints=(MINT, OTHER_MINT)
    )
    original_events = rows(factory, scout_events)
    original_lifecycles = rows(factory, token_lifecycles)
    apply = LifecycleRepository.apply
    attempted: list[str] = []

    def fail_second(self: LifecycleRepository, event: ScoutEventRecord) -> Any:
        attempted.append(event.event_key)
        if len(attempted) == 2:
            raise RuntimeError("fixture lifecycle write failure")
        return apply(self, event)

    monkeypatch.setattr(LifecycleRepository, "apply", fail_second)
    with pytest.raises(RuntimeError, match="fixture lifecycle write failure"):
        reconcile(factory, LookupSpy(envelope()), DecoderSpy(events))
    assert len(attempted) == 2
    assert rows(factory, reconciliation_records) == []
    assert rows(factory, scout_events) == original_events
    assert rows(factory, token_lifecycles) == original_lifecycles
    durable = rows(factory, raw_events)
    assert len(durable) == 2
    assert durable[-1]["commitment"] == "finalized"


def test_actual_lifecycle_sql_failure_rolls_back_all_corrections_and_keeps_raw_durable(
    factory: sessionmaker[Session],
) -> None:
    events, _ = observe(
        factory, event_types=("token_created", "token_created"), mints=(MINT, OTHER_MINT)
    )
    original_events = rows(factory, scout_events)
    original_lifecycles = rows(factory, token_lifecycles)
    with factory() as session:
        session.execute(
            text(
                "CREATE TRIGGER fail_finality_lifecycle BEFORE UPDATE ON token_lifecycles "
                f"WHEN NEW.mint = '{OTHER_MINT}' "
                "BEGIN SELECT RAISE(ABORT, 'fixture lifecycle SQL failure'); END"
            )
        )
        session.commit()
    with pytest.raises(IntegrityError, match="fixture lifecycle SQL failure"):
        reconcile(factory, LookupSpy(envelope()), DecoderSpy(events))
    assert rows(factory, reconciliation_records) == []
    assert rows(factory, scout_events) == original_events
    assert rows(factory, token_lifecycles) == original_lifecycles
    durable = rows(factory, raw_events)
    assert len(durable) == 2
    assert durable[-1]["commitment"] == "finalized"
    assert durable[-1]["status"] == "decoded"


@pytest.mark.parametrize("complete", ["true", "false", 1, 0, None])
def test_complete_decode_claim_must_be_an_explicit_boolean(complete: object) -> None:
    with pytest.raises(ValidationError):
        FinalizedDecodeResult(status=DecodeStatus.DECODED, complete=complete)
