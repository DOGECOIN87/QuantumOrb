"""Offline acceptance tests for the fixture transport and replay CLI."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import raw_events
from app.db.raw_journal import RawJournal
from app.db.session import create_database_engine
from app.integrations.fixture_transport import FixtureTransport, FixtureTransportError
from scripts.replay_fixture import main as replay_fixture_main

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_HELIUS_FIXTURES = _REPOSITORY_ROOT / "coding-pack" / "fixtures" / "helius"
_MALFORMED_FIXTURES = _REPOSITORY_ROOT / "coding-pack" / "fixtures" / "malformed"


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session


def test_checked_in_provider_fixtures_validate_through_the_shared_envelope() -> None:
    fixtures = FixtureTransport([_HELIUS_FIXTURES]).load_all()

    assert len(fixtures) == 4
    assert {item.envelope.transport.value for item in fixtures} == {"fixture", "logs_subscribe"}
    assert all(item.envelope.provider == "helius" for item in fixtures)


def test_all_provider_fixtures_replayed_twice_create_no_duplicate_rows(db: Session) -> None:
    fixtures = FixtureTransport([_HELIUS_FIXTURES]).load_all()
    journal = RawJournal(db)

    first_pass = [journal.insert(item.envelope) for item in fixtures]
    second_pass = [journal.insert(item.envelope) for item in fixtures]

    assert [record.id for record in first_pass] == [record.id for record in second_pass]
    assert db.scalar(select(func.count()).select_from(raw_events)) == len(fixtures)


def test_malformed_fixture_files_are_rejected_before_any_database_write(db: Session) -> None:
    with pytest.raises(FixtureTransportError, match="invalid provider envelope fixture"):
        FixtureTransport([_MALFORMED_FIXTURES]).load_all()

    assert db.scalar(select(func.count()).select_from(raw_events)) == 0


def test_missing_fixture_path_fails_with_a_safe_diagnostic() -> None:
    with pytest.raises(FixtureTransportError, match="does not exist"):
        FixtureTransport(["/path/that/does/not/exist.json"]).load_all()


def test_cli_replays_twice_and_reports_stable_rows_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("fixture replay must not access the network")

    monkeypatch.setattr(socket, "create_connection", no_network)
    database_path = tmp_path / "replay.db"
    engine = create_database_engine(f"sqlite:///{database_path}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()

    result = replay_fixture_main(
        ["--database-url", f"sqlite:///{database_path}", str(_HELIUS_FIXTURES)]
    )

    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert result == 0
    assert summary == {
        "fixture_count": 4,
        "replay_passes": 2,
        "stable_fixture_rows": 4,
        "status": "ok",
        "total_raw_event_rows": 4,
    }
    assert captured.err == ""
