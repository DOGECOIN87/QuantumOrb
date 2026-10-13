"""Offline acceptance tests for the provider-neutral decoder boundary."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.base import Base
from app.db.decode_outcomes import (
    DecodeOutcomeConflictError,
    DecodeOutcomeRepository,
)
from app.db.models import raw_events, scout_events
from app.db.raw_journal import RawJournal
from app.decoders import (
    DecodePipeline,
    DecodeResult,
    DecoderInput,
    FixtureDecoder,
)
from app.domain.enums import DecodeStatus
from app.domain.models import ProviderEnvelope
from app.integrations.fixture_transport import FixtureTransport

PUMP_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM_ID = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
SIGNATURE = "1" * 64
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE_DIR = _REPOSITORY_ROOT / "coding-pack" / "fixtures" / "helius"


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session


def settings() -> Settings:
    return Settings(
        ingest_mode="demo",
        ingest_transport="fixture",
        pump_program_id=PUMP_ID,
        pump_amm_program_id=PUMP_AMM_ID,
    )


def make_envelope(
    payload: dict[str, object],
    *,
    transport: str = "fixture",
    notification_id: str = "decoder-fixture-001",
) -> ProviderEnvelope:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return ProviderEnvelope(
        transport=transport,
        notification_id=notification_id,
        received_at="2026-10-13T01:00:00Z",
        commitment="confirmed",
        slot=300_000_001,
        signature=SIGNATURE,
        raw_payload_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        idl_revision="fixture-pump-idl-1",
        raw_payload=payload,
    )


def pipeline(db: Session, decoder: FixtureDecoder | None = None) -> DecodePipeline:
    return DecodePipeline(
        RawJournal(db),
        decoder or FixtureDecoder(settings()),
        DecodeOutcomeRepository(db),
    )


def test_known_event_is_typed_but_not_scout_classified(db: Session) -> None:
    payload = {
        "program": "pump",
        "eventName": "CreateEvent",
        "eventIndex": 0,
        "decoded": {
            "mint": "7LSsEoJGhLeZzGvDofTdNg7M3JttxQqGWNLo6vWMpump",
            "token_total_supply": "1000000000000000",
        },
    }

    result = pipeline(db).process(make_envelope(payload))

    assert result.status == DecodeStatus.DECODED
    assert result.events[0].event_name == "CreateEvent"
    assert result.events[0].program_id == PUMP_ID
    assert result.events[0].fields["token_total_supply"] == "1000000000000000"
    assert result.events[0].decode_status == DecodeStatus.DECODED
    assert db.scalar(select(func.count()).select_from(scout_events)) == 0
    stored = db.execute(select(raw_events)).one()._mapping
    assert stored["status"] == DecodeStatus.DECODED.value
    assert stored["safe_error_code"] is None


def test_unknown_event_is_stored_raw_only_and_never_classified(db: Session) -> None:
    payload = {
        "program": "pump",
        "eventName": "FutureEvent",
        "decoded": {"mint": "future-value"},
    }
    envelope = make_envelope(payload)

    result = pipeline(db).process(envelope)
    repeated = pipeline(db).process(envelope)

    assert result.status == DecodeStatus.RAW_ONLY
    assert result.unknown_event_name == "FutureEvent"
    assert result.events == []
    assert result.instructions == []
    assert repeated.raw_event_id == result.raw_event_id
    assert db.scalar(select(func.count()).select_from(raw_events)) == 1
    assert db.scalar(select(func.count()).select_from(scout_events)) == 0
    stored = db.execute(select(raw_events)).one()._mapping
    assert stored["status"] == DecodeStatus.RAW_ONLY.value
    assert stored["safe_error_code"] == "unknown_event"
    assert json.loads(stored["payload_json"]) == payload
    assert stored["payload_sha256"] == hashlib.sha256(stored["payload_json"].encode()).hexdigest()


def test_malformed_known_event_is_failed_without_classification(db: Session) -> None:
    payload = {"program": "pump", "eventName": "CreateEvent", "decoded": ["not", "an object"]}

    result = pipeline(db).process(make_envelope(payload))

    assert result.status == DecodeStatus.FAILED
    assert result.safe_error_code == "malformed_event"
    assert result.events == []
    assert db.scalar(select(func.count()).select_from(scout_events)) == 0
    stored = db.execute(select(raw_events)).one()._mapping
    assert stored["status"] == DecodeStatus.FAILED.value
    assert stored["safe_error_code"] == "malformed_event"
    assert json.loads(stored["payload_json"]) == payload


def test_program_id_mismatch_fails_closed(db: Session) -> None:
    payload = {
        "program": "pump",
        "programId": PUMP_AMM_ID,
        "eventName": "CreateEvent",
        "eventIndex": 0,
        "decoded": {"mint": "opaque"},
    }

    result = pipeline(db).process(make_envelope(payload))

    assert result.status == DecodeStatus.FAILED
    assert result.safe_error_code == "program_id_mismatch"
    assert result.events == []
    assert db.scalar(select(func.count()).select_from(scout_events)) == 0


def test_non_fixture_transport_is_retained_raw_only(db: Session) -> None:
    result = pipeline(db).process(
        make_envelope({"method": "logsNotification"}, transport="logs_subscribe")
    )

    assert result.status == DecodeStatus.RAW_ONLY
    assert result.safe_error_code == "unsupported_transport"
    assert result.events == []
    assert db.execute(select(raw_events.c.status)).scalar_one() == DecodeStatus.RAW_ONLY.value


def test_pipeline_persists_the_payload_before_calling_decoder(db: Session) -> None:
    fixture_decoder = FixtureDecoder(settings())

    class AssertStoredDecoder:
        def decode(self, event: DecoderInput) -> DecodeResult:
            stored = db.execute(
                select(raw_events.c.payload_json).where(raw_events.c.id == event.raw_event_id)
            ).scalar_one_or_none()
            assert stored == event.payload_json
            return fixture_decoder.decode(event)

    payload = {
        "program": "pump",
        "eventName": "CreateEvent",
        "eventIndex": 0,
        "decoded": {"mint": "opaque"},
    }

    result = DecodePipeline(
        RawJournal(db),
        AssertStoredDecoder(),
        DecodeOutcomeRepository(db),
    ).process(make_envelope(payload))

    assert result.status == DecodeStatus.DECODED
    assert db.scalar(select(func.count()).select_from(raw_events)) == 1


def test_checked_in_provider_fixtures_use_the_same_decoder_interface(db: Session) -> None:
    transport = FixtureTransport([_FIXTURE_DIR])
    process = pipeline(db)

    results = [process.process(item.envelope) for item in transport.load_all()]

    assert [result.status for result in results].count(DecodeStatus.DECODED) == 3
    assert [result.status for result in results].count(DecodeStatus.RAW_ONLY) == 1
    assert db.scalar(select(func.count()).select_from(raw_events)) == 4
    assert db.scalar(select(func.count()).select_from(scout_events)) == 0


def test_decode_outcome_cannot_replace_a_completed_decoded_state(db: Session) -> None:
    payload = {
        "program": "pump",
        "eventName": "CreateEvent",
        "eventIndex": 0,
        "decoded": {"mint": "opaque"},
    }
    result = pipeline(db).process(make_envelope(payload))

    with pytest.raises(DecodeOutcomeConflictError):
        DecodeOutcomeRepository(db).record_outcome(
            raw_event_id=result.raw_event_id,
            status=DecodeStatus.RAW_ONLY,
            safe_error_code="unknown_event",
            safe_error_message="event is not present in the pinned program IDL",
        )
