"""Offline durable stream-cursor and atomic-journaling acceptance tests."""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.models import cursors, raw_events
from app.db.raw_journal import RawEventRecord, RawJournal
from app.db.stream_cursors import StreamCursor, StreamCursorError, StreamCursorRepository
from app.domain.models import ProviderEnvelope

PUMP = "6EF8rrecthR5DkzPWsYzX9Z9u18RYPWv9VKn6kXHEfU"
AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
SIG = "1" * 64
SIG2 = "2" + "1" * 87
NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)
TRANSPORT = "logs_subscribe"


@pytest.fixture
def factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine(f"sqlite:///{tmp_path / 'cursor-test.db'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


def envelope(
    slot: int = 42,
    *,
    identity: str | None = None,
    signature: str = SIG,
    received_at: datetime = NOW,
    transport: str = TRANSPORT,
) -> ProviderEnvelope:
    payload = {"slot": slot, "identity": identity or str(slot)}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return ProviderEnvelope(
        transport=transport,
        notification_id=identity or str(slot),
        received_at=received_at,
        commitment="confirmed",
        slot=slot,
        signature=signature,
        raw_payload=payload,
        raw_payload_sha256=hashlib.sha256(encoded.encode()).hexdigest(),
    )


def stored(factory: sessionmaker[Session]) -> dict[str, Any]:
    with factory() as session:
        row = session.execute(select(cursors)).one()._mapping
        return dict(row)


def test_journal_and_cursor_share_transaction_and_survive_restart(
    factory: sessionmaker[Session],
) -> None:
    repo = StreamCursorRepository(factory)
    assert repo.load(TRANSPORT, PUMP) is None
    row = repo.journal(envelope(), PUMP)
    cursor = StreamCursorRepository(factory).load(TRANSPORT, PUMP)
    assert cursor == StreamCursor(TRANSPORT, PUMP, 42, SIG, NOW, row.id)
    values = stored(factory)
    assert values["name"] == f"stream:mainnet-beta:{TRANSPORT}:{PUMP}"
    assert values["last_received_slot"] == 42
    assert values["last_received_signature"] == SIG
    with pytest.raises(FrozenInstanceError):
        cursor.slot = 43  # type: ignore[misc, union-attr]


def test_higher_slot_advances_without_regressing_on_late_equal_or_duplicate(
    factory: sessionmaker[Session],
) -> None:
    repo = StreamCursorRepository(factory)
    repo.journal(envelope(), PUMP)
    new = repo.journal(envelope(45, received_at=NOW + timedelta(seconds=5)), PUMP)
    high = repo.load(TRANSPORT, PUMP)
    assert high is not None and high.raw_event_id == new.id
    repo.journal(envelope(43, received_at=NOW + timedelta(seconds=6)), PUMP)
    repo.journal(
        envelope(45, identity="equal-slot", signature=SIG2, received_at=NOW + timedelta(seconds=7)),
        PUMP,
    )
    repo.journal(envelope(45, received_at=NOW + timedelta(seconds=9)), PUMP)
    assert repo.load(TRANSPORT, PUMP) == high
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(raw_events)) == 4


def test_program_and_transport_observations_are_independent(
    factory: sessionmaker[Session],
) -> None:
    repo = StreamCursorRepository(factory)
    repo.journal(envelope(10), PUMP)
    repo.journal(envelope(20), AMM)
    repo.journal(envelope(30, transport="parsed_streams"), PUMP)
    assert repo.load(TRANSPORT, PUMP).slot == 10  # type: ignore[union-attr]
    assert repo.load(TRANSPORT, AMM).slot == 20  # type: ignore[union-attr]
    assert repo.load("parsed_streams", PUMP).slot == 30  # type: ignore[union-attr]


def test_begin_freezes_anchor_while_new_observations_and_restart_preserve_it(
    factory: sessionmaker[Session],
) -> None:
    repo = StreamCursorRepository(factory)
    row = repo.journal(envelope(), PUMP)
    began = repo.begin_recovery(TRANSPORT, PUMP, NOW + timedelta(seconds=5))
    assert began.pending_since == NOW + timedelta(seconds=5)
    assert began.anchor_slot == 42
    assert began.anchor_signature == SIG
    assert began.anchor_received_at == NOW
    repo.journal(envelope(50, signature=SIG2, received_at=NOW + timedelta(seconds=10)), PUMP)
    restarted = StreamCursorRepository(factory)
    again = restarted.begin_recovery(TRANSPORT, PUMP, NOW + timedelta(seconds=30))
    assert again.pending_since == began.pending_since
    assert again.anchor_slot == began.anchor_slot
    assert again.anchor_signature == began.anchor_signature
    assert again.anchor_received_at == began.anchor_received_at
    assert again.slot == 50 and again.signature == SIG2 and again.raw_event_id != row.id


def test_first_connection_records_coverage_start_before_any_observations(
    factory: sessionmaker[Session],
) -> None:
    repo = StreamCursorRepository(factory)
    began = repo.begin_recovery(TRANSPORT, PUMP, NOW)
    assert began.slot is began.signature is began.raw_event_id is None
    assert began.pending_since == began.anchor_received_at == began.received_at == NOW
    assert began.anchor_slot is began.anchor_signature is None
    repo.journal(envelope(1, received_at=NOW + timedelta(seconds=10)), PUMP)
    current = repo.load(TRANSPORT, PUMP)
    assert current is not None and current.slot == 1
    assert current.anchor_slot is None and current.anchor_received_at == NOW
    assert current.received_at == NOW + timedelta(seconds=10)


def test_finish_preserves_new_live_observation_and_gap_is_sticky(
    factory: sessionmaker[Session],
) -> None:
    repo = StreamCursorRepository(factory)
    repo.journal(envelope(), PUMP)
    repo.begin_recovery(TRANSPORT, PUMP, NOW + timedelta(seconds=1))
    repo.journal(envelope(50, received_at=NOW + timedelta(seconds=2)), PUMP)
    finished = repo.finish_recovery(
        TRANSPORT, PUMP, now=NOW + timedelta(seconds=3), gap_reason="signature_limit"
    )
    assert finished.slot == 50
    assert finished.data_gap and finished.gap_reason == "signature_limit"
    assert finished.pending_since is finished.anchor_slot is finished.anchor_signature is None
    assert finished.anchor_received_at is None
    repo.begin_recovery(TRANSPORT, PUMP, NOW + timedelta(seconds=4))
    covered = repo.finish_recovery(TRANSPORT, PUMP, now=NOW + timedelta(seconds=5), gap_reason=None)
    assert covered.data_gap and covered.gap_reason == "signature_limit"
    assert stored(factory)["last_success_at"] == (NOW + timedelta(seconds=5)).replace(tzinfo=None)


def test_finish_without_prior_cursor_is_rejected(factory: sessionmaker[Session]) -> None:
    with pytest.raises(StreamCursorError, match="does not exist"):
        StreamCursorRepository(factory).finish_recovery(TRANSPORT, PUMP, now=NOW, gap_reason=None)


def test_callback_failure_rolls_back_new_raw_and_cursor(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = StreamCursorRepository(factory)
    original = repo._write

    def fail(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        raise RuntimeError("test transaction failure")

    monkeypatch.setattr(repo, "_write", fail)
    with pytest.raises(RuntimeError, match="test transaction failure"):
        repo.journal(envelope(), PUMP)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(raw_events)) == 0
        assert session.scalar(select(func.count()).select_from(cursors)) == 0


def test_failed_cursor_update_retains_both_previous_high_and_previous_raw(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = StreamCursorRepository(factory)
    first = repo.journal(envelope(), PUMP)
    initial = repo.load(TRANSPORT, PUMP)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise StreamCursorError("test cursor failure")

    monkeypatch.setattr(repo, "_write", fail)
    with pytest.raises(StreamCursorError, match="test cursor failure"):
        repo.journal(envelope(50), PUMP)
    assert repo.load(TRANSPORT, PUMP) == initial
    with factory() as session:
        assert session.scalars(select(raw_events.c.id)).all() == [first.id]


def test_callback_runs_on_duplicates_before_commit_and_failure_rolls_back(
    factory: sessionmaker[Session],
) -> None:
    journal = RawJournal(factory)
    first = journal.insert(envelope())
    seen: list[int] = []

    def callback(session: Session, row: RawEventRecord) -> None:
        assert session.scalar(select(raw_events.c.id)) == first.id
        seen.append(row.id)
        session.execute(update(raw_events).values(status="failed"))
        raise RuntimeError("test callback failure")

    with pytest.raises(RuntimeError, match="test callback failure"):
        journal.insert(envelope(received_at=NOW + timedelta(seconds=1)), before_commit=callback)
    assert seen == [first.id]
    with factory() as session:
        assert session.scalar(select(raw_events.c.status)) == "new"


def test_new_callback_reads_uncommitted_raw_and_commits_with_it(
    factory: sessionmaker[Session],
) -> None:
    seen: list[int] = []

    def callback(session: Session, row: RawEventRecord) -> None:
        assert session.scalar(select(raw_events.c.id)) == row.id
        with factory() as independent:
            assert independent.scalar(select(func.count()).select_from(raw_events)) == 0
        seen.append(row.id)
        session.execute(update(raw_events).values(status="raw_only"))

    row = RawJournal(factory).insert(envelope(), before_commit=callback)
    assert seen == [row.id]
    with factory() as session:
        assert session.scalar(select(raw_events.c.status)) == "raw_only"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", 2),
        ("version", True),
        ("slot", True),
        ("slot", -1),
        ("slot", None),
        ("signature", "not a signature"),
        ("raw_event_id", None),
        ("raw_event_id", True),
        ("raw_event_id", 0),
        ("transport", "fixture"),
        ("transport", "parsed_streams"),
        ("program_id", AMM),
        ("received_at", "2026-10-13T12:00:00"),
        ("received_at", None),
        ("received_at", 1),
        ("pending_since", NOW.isoformat()),
        ("anchor_slot", 20),
        ("anchor_received_at", NOW.isoformat()),
        ("data_gap", True),
        ("data_gap", "false"),
        ("gap_reason", "provider failure secret"),
        ("unexpected", 1),
    ],
)
def test_corrupt_cursor_json_is_rejected(
    factory: sessionmaker[Session], field: str, value: Any
) -> None:
    repo = StreamCursorRepository(factory)
    repo.journal(envelope(), PUMP)
    values = json.loads(stored(factory)["cursor_json"])
    values[field] = value
    with factory() as session:
        session.execute(update(cursors).values(cursor_json=json.dumps(values)))
        session.commit()
    with pytest.raises(StreamCursorError, match="persisted stream cursor is invalid"):
        repo.load(TRANSPORT, PUMP)
    with pytest.raises(StreamCursorError):
        repo.journal(envelope(43), PUMP)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(raw_events)) == 1


@pytest.mark.parametrize("value", ["{}", "[]", "null", "{", "x" * 4097])
def test_invalid_cursor_document_is_rejected(factory: sessionmaker[Session], value: str) -> None:
    repo = StreamCursorRepository(factory)
    repo.journal(envelope(), PUMP)
    with factory() as session:
        session.execute(update(cursors).values(cursor_json=value))
        session.commit()
    with pytest.raises(StreamCursorError):
        repo.load(TRANSPORT, PUMP)


def test_duplicate_cursor_json_keys_and_inconsistent_columns_rejected(
    factory: sessionmaker[Session],
) -> None:
    repo = StreamCursorRepository(factory)
    repo.journal(envelope(), PUMP)
    encoded = stored(factory)["cursor_json"]
    with factory() as session:
        duplicated = encoded.replace('"version":1', '"version":1,"version":1')
        session.execute(update(cursors).values(cursor_json=duplicated))
        session.commit()
    with pytest.raises(StreamCursorError):
        repo.load(TRANSPORT, PUMP)
    with factory() as session:
        session.execute(update(cursors).values(cursor_json=encoded, last_received_slot=100))
        session.commit()
    with pytest.raises(StreamCursorError):
        repo.load(TRANSPORT, PUMP)


@pytest.mark.parametrize("transport", ["fixture", "http", "", "logs_subscribe "])
def test_unsupported_transport_is_rejected(factory: sessionmaker[Session], transport: str) -> None:
    with pytest.raises(StreamCursorError):
        StreamCursorRepository(factory).begin_recovery(transport, PUMP, NOW)


@pytest.mark.parametrize("program_id", ["", "invalid-key", "1" * 31, "1" * 33])
def test_invalid_program_is_rejected(factory: sessionmaker[Session], program_id: str) -> None:
    with pytest.raises(StreamCursorError):
        StreamCursorRepository(factory).journal(envelope(), program_id)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(raw_events)) == 0


@pytest.mark.parametrize(
    "reason", ["", "contains space", "SECRET=private", "x" * 65, "bad\nreason"]
)
def test_safe_gap_reason_only(factory: sessionmaker[Session], reason: str) -> None:
    repo = StreamCursorRepository(factory)
    began = repo.begin_recovery(TRANSPORT, PUMP, NOW)
    with pytest.raises(StreamCursorError, match="safe identifier"):
        repo.finish_recovery(TRANSPORT, PUMP, now=NOW, gap_reason=reason)
    assert repo.load(TRANSPORT, PUMP) == began


def test_timestamps_require_timezone_and_are_normalized(factory: sessionmaker[Session]) -> None:
    repo = StreamCursorRepository(factory)
    with pytest.raises(StreamCursorError, match="timezone-aware"):
        repo.begin_recovery(TRANSPORT, PUMP, NOW.replace(tzinfo=None))
    offset = timezone(timedelta(hours=-5))
    began = repo.begin_recovery(TRANSPORT, PUMP, NOW.astimezone(offset))
    assert began.received_at == NOW and began.received_at.tzinfo == UTC
    with pytest.raises(StreamCursorError, match="timezone-aware"):
        repo.finish_recovery(TRANSPORT, PUMP, now=NOW.replace(tzinfo=None), gap_reason=None)


def test_existing_shared_session_works_and_is_not_closed(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        repo = StreamCursorRepository(session)
        repo.journal(envelope(), PUMP)
        assert repo.load(TRANSPORT, PUMP) is not None
        repo.begin_recovery(TRANSPORT, PUMP, NOW)
        repo.finish_recovery(TRANSPORT, PUMP, now=NOW, gap_reason=None)
        assert session.scalar(select(func.count()).select_from(raw_events)) == 1


def test_mark_gap_preserves_pending_anchor_across_restart_and_live_observations(
    factory: sessionmaker[Session],
) -> None:
    repo = StreamCursorRepository(factory)
    repo.journal(envelope(), PUMP)
    began = repo.begin_recovery(TRANSPORT, PUMP, NOW + timedelta(seconds=1))
    repo.journal(envelope(50, received_at=NOW + timedelta(seconds=2)), PUMP)
    marked = repo.mark_gap(
        TRANSPORT, PUMP, now=NOW + timedelta(seconds=3), gap_reason="recovery_cancelled"
    )
    assert marked.slot == 50
    assert marked.pending_since == began.pending_since
    assert marked.anchor_slot == began.anchor_slot
    assert marked.anchor_signature == began.anchor_signature
    assert marked.anchor_received_at == began.anchor_received_at
    assert marked.data_gap and marked.gap_reason == "recovery_cancelled"
    restarted = StreamCursorRepository(factory)
    resumed = restarted.begin_recovery(TRANSPORT, PUMP, NOW + timedelta(seconds=4))
    assert resumed == marked
    finished = restarted.finish_recovery(
        TRANSPORT, PUMP, now=NOW + timedelta(seconds=5), gap_reason=None
    )
    assert finished.pending_since is None
    assert finished.data_gap and finished.gap_reason == "recovery_cancelled"
