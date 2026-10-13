"""Offline golden tests for Pump CreateEvent canonical normalization."""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.base import Base
from app.db.raw_journal import RawJournal
from app.decoders import DecoderInput, FixtureDecoder
from app.domain.enums import Commitment, DecodeStatus, FinalityStatus, ReconciliationStatus
from app.domain.models import DecodedEvent, ProviderEnvelope
from app.integrations.fixture_transport import FixtureTransport
from app.normalization import (
    EventNormalizationError,
    PumpCreateEventNormalizer,
    UnsupportedNormalizationError,
)

PUMP_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM_ID = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE_DIR = _REPOSITORY_ROOT / "coding-pack" / "fixtures" / "helius"
_GOLDEN_PATH = _REPOSITORY_ROOT / "coding-pack" / "golden-tests" / "create-event.expected.json"
SIGNATURE = "1" * 64


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


def create_fixture() -> ProviderEnvelope:
    fixture = next(
        item.envelope
        for item in FixtureTransport([_FIXTURE_DIR]).load_all()
        if item.envelope.raw_payload.get("program") == "pump"
        and item.envelope.raw_payload.get("eventName") == "CreateEvent"
    )
    return fixture


def decode_persisted_fixture(db: Session) -> tuple[DecodedEvent, ProviderEnvelope, int]:
    envelope = create_fixture()
    raw = RawJournal(db).insert(envelope)
    decoded = FixtureDecoder(settings()).decode(
        DecoderInput(
            raw_event_id=raw.id,
            transport=raw.transport,
            payload_json=raw.payload_json,
        )
    )
    assert decoded.status == DecodeStatus.DECODED
    assert len(decoded.events) == 1
    return decoded.events[0], envelope, raw.id


def normalize(
    event: DecodedEvent,
    *,
    raw_event_id: int = 1,
    commitment: Commitment = Commitment.CONFIRMED,
):
    envelope = create_fixture()
    return PumpCreateEventNormalizer(settings()).normalize(
        event,
        provider=envelope.provider,
        signature=envelope.signature,
        slot=envelope.slot,
        commitment=commitment,
        observed_at=envelope.received_at,
        raw_event_id=raw_event_id,
    )


def test_fixture_decoder_and_normalizer_match_create_event_golden(db: Session) -> None:
    decoded, envelope, raw_event_id = decode_persisted_fixture(db)
    canonical = PumpCreateEventNormalizer(settings()).normalize(
        decoded,
        provider=envelope.provider,
        signature=envelope.signature,
        slot=envelope.slot,
        commitment=envelope.commitment,
        observed_at=envelope.received_at,
        raw_event_id=raw_event_id,
    )
    expected = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
    projection = {
        "event_type": canonical.event_type.value,
        "program": canonical.program.value,
        "event_name": canonical.event_name,
        "mint": canonical.mint,
        "bonding_curve": canonical.bonding_curve,
        "user": canonical.user,
        "creator": canonical.creator,
        "quote_mint": canonical.quote_mint,
        "decode_status": canonical.decode_status.value,
    }

    assert projection == expected
    assert canonical.user != canonical.creator
    assert canonical.raw_event_id == raw_event_id
    assert canonical.block_time is not None
    assert canonical.block_time.tzinfo is UTC
    assert canonical.finality_status == FinalityStatus.PROVISIONAL
    assert canonical.reconciliation_status == ReconciliationStatus.PENDING
    assert "virtual_token_reserves" in canonical.payload


def test_missing_appended_fields_remain_absent_without_fallbacks() -> None:
    envelope = create_fixture()
    fields = dict(envelope.raw_payload["decoded"])
    appended = (
        "token_program",
        "is_mayhem_mode",
        "is_cashback_enabled",
        "quote_mint",
        "virtual_quote_reserves",
        "creator_fee_bps",
        "is_holder_reward",
        "depth",
    )
    for name in appended:
        fields.pop(name, None)
    event = DecodedEvent(
        event_name="CreateEvent",
        program_id=PUMP_ID,
        event_index=0,
        fields=fields,
        decode_status=DecodeStatus.DECODED,
    )

    canonical = normalize(event)

    assert canonical.quote_mint is None
    assert "quote_mint" not in canonical.payload
    assert all(name not in canonical.payload for name in appended)
    assert canonical.user == fields["user"]
    assert canonical.creator == fields["creator"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda fields: fields.pop("mint"),
        lambda fields: fields.__setitem__("mint", "not-base58"),
        lambda fields: fields.__setitem__("virtual_token_reserves", "18446744073709551616"),
        lambda fields: fields.__setitem__("timestamp", True),
        lambda fields: fields.__setitem__("depth", 256),
    ],
    ids=("missing-required", "invalid-public-key", "u64-overflow", "bool-timestamp", "u8-overflow"),
)
def test_malformed_create_fields_are_not_normalized(mutation) -> None:
    fields = dict(create_fixture().raw_payload["decoded"])
    mutation(fields)
    event = DecodedEvent(
        event_name="CreateEvent",
        program_id=PUMP_ID,
        event_index=0,
        fields=fields,
        decode_status=DecodeStatus.DECODED,
    )

    with pytest.raises(EventNormalizationError, match="fields are malformed"):
        normalize(event)


def test_only_decoded_pump_create_event_has_a_mapping() -> None:
    event = DecodedEvent(
        event_name="TradeEvent",
        program_id=PUMP_ID,
        event_index=0,
        fields={},
        decode_status=DecodeStatus.DECODED,
    )

    with pytest.raises(UnsupportedNormalizationError):
        normalize(event)


def test_finalized_commitment_is_not_marked_provisional() -> None:
    fields = dict(create_fixture().raw_payload["decoded"])
    event = DecodedEvent(
        event_name="CreateEvent",
        program_id=PUMP_ID,
        event_index=0,
        fields=fields,
        decode_status=DecodeStatus.DECODED,
    )

    canonical = normalize(event, commitment=Commitment.FINALIZED)

    assert canonical.finality_status == FinalityStatus.FINALIZED
    assert canonical.reconciliation_status == ReconciliationStatus.NOT_REQUIRED


def test_source_instruction_index_maps_to_instruction_index() -> None:
    fields = dict(create_fixture().raw_payload["decoded"])
    event = DecodedEvent(
        event_name="CreateEvent",
        program_id=PUMP_ID,
        event_index=2,
        source_instruction_index=7,
        fields=fields,
        decode_status=DecodeStatus.DECODED,
    )

    canonical = normalize(event)

    assert canonical.event_index == 2
    assert canonical.instruction_index == 7
