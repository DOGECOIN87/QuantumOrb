"""PumpSwap pool golden vector and exact signed reserve normalization."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.base import Base
from app.db.raw_journal import RawJournal
from app.decoders import DecoderInput, FixtureDecoder
from app.domain.enums import Commitment, DecodeStatus
from app.domain.models import DecodedEvent
from app.integrations.fixture_transport import FixtureTransport
from app.normalization import EventNormalizationError, UnsupportedNormalizationError
from app.pumpswap import PumpSwapEventNormalizer

ROOT = Path(__file__).resolve().parents[2]
ADDRESS = "So11111111111111111111111111111111111111112"


def normalize(event, **overrides):
    metadata = dict(
        provider="helius",
        signature="1" * 64,
        slot=1,
        commitment=Commitment.CONFIRMED,
        observed_at=datetime.now(UTC),
        raw_event_id=1,
    )
    metadata.update(overrides)
    return PumpSwapEventNormalizer(Settings()).normalize(event, **metadata)


def trade(name="BuyEvent", virtual="-10"):
    return DecodedEvent(
        event_name=name,
        program_id=Settings().pump_amm_program_id,
        event_index=0,
        decode_status=DecodeStatus.DECODED,
        fields=dict(
            timestamp=1791853260,
            pool=ADDRESS,
            user=ADDRESS,
            pool_base_token_reserves="100",
            pool_quote_token_reserves="20",
            virtual_quote_reserves=virtual,
            **(
                {"base_amount_out": "3", "quote_amount_in": "4"}
                if name == "BuyEvent"
                else {"base_amount_in": "3", "quote_amount_out": "4"}
            ),
        ),
    )


def test_pool_golden_and_duplicate_journal():
    envelope = (
        FixtureTransport([ROOT / "coding-pack/fixtures/helius/pumpswap-create-pool-event.json"])
        .load_all()[0]
        .envelope
    )
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        journal = RawJournal(session)
        row = journal.insert(envelope)
        assert journal.insert(envelope).id == row.id
        decoded = FixtureDecoder(Settings()).decode(
            DecoderInput(
                raw_event_id=row.id, transport=row.transport, payload_json=row.payload_json
            )
        )
        result = normalize(decoded.events[0], raw_event_id=row.id, commitment=Commitment.FINALIZED)
        golden = json.loads(
            (ROOT / "coding-pack/golden-tests/pool-event.expected.json").read_text()
        )
        output = result.model_dump(mode="json")
        output["base_mint"] = result.mint
        assert {key: output[key] for key in golden} == golden
        assert result.user == envelope.raw_payload["decoded"]["creator"]
        assert result.creator == envelope.raw_payload["decoded"]["coin_creator"]
        assert result.finality_status == "finalized"
    engine.dispose()


@pytest.mark.parametrize("name", ["BuyEvent", "SellEvent"])
@pytest.mark.parametrize("virtual", ["0", "10", "-10", str(-(1 << 127)), str((1 << 127) - 1)])
def test_trades_signed_reserves(name, virtual):
    event = trade(name, virtual)
    result = normalize(event)
    assert result.reserves["effective_quote_reserves"] == str(20 + int(virtual))
    assert result.event_type == ("pool_buy" if name == "BuyEvent" else "pool_sell")
    assert result.mint is None and result.quote_mint is None
    assert result.amounts["base_amount"] == "3"
    assert normalize(event).event_key == result.event_key


@pytest.mark.parametrize(
    "field,value",
    [
        ("virtual_quote_reserves", str(1 << 127)),
        ("virtual_quote_reserves", str(-(1 << 127) - 1)),
        ("virtual_quote_reserves", "-0"),
        ("virtual_quote_reserves", "01"),
        ("virtual_quote_reserves", 1.5),
        ("pool_quote_token_reserves", str(1 << 64)),
        ("pool_quote_token_reserves", "-1"),
        ("user", "invalid"),
        ("timestamp", True),
        ("timestamp", 1 << 63),
        ("timestamp", (1 << 63) - 1),
        ("lp_fee", 1),
        ("can_boost", "false"),
    ],
)
def test_malformed_fields(field, value):
    event = trade()
    event.fields[field] = value
    with pytest.raises(EventNormalizationError, match="malformed"):
        normalize(event)


@pytest.mark.parametrize(
    "field", ["user", "pool", "timestamp", "virtual_quote_reserves", "quote_amount_in"]
)
def test_missing_fields(field):
    event = trade()
    del event.fields[field]
    with pytest.raises(EventNormalizationError):
        normalize(event)


@pytest.mark.parametrize(
    "metadata",
    [
        {"raw_event_id": True},
        {"slot": -1},
        {"signature": ""},
        {"observed_at": datetime(2026, 1, 1)},
    ],
)
def test_metadata(metadata):
    with pytest.raises(EventNormalizationError):
        normalize(trade(), **metadata)


def test_unsupported_and_failed():
    with pytest.raises(UnsupportedNormalizationError):
        normalize(trade().model_copy(update={"event_name": "UnknownEvent"}))
    with pytest.raises(UnsupportedNormalizationError):
        normalize(trade().model_copy(update={"program_id": Settings().pump_program_id}))
    with pytest.raises(EventNormalizationError):
        normalize(trade().model_copy(update={"decode_status": DecodeStatus.FAILED}))
