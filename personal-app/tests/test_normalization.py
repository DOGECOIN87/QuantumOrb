"""Offline golden tests for Pump CreateEvent canonical normalization."""

from __future__ import annotations

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
from app.domain.enums import (
    Commitment,
    DecodeStatus,
    FinalityStatus,
    ReconciliationStatus,
    ScoutEventType,
)
from app.domain.models import DecodedEvent, ProviderEnvelope
from app.integrations.fixture_transport import FixtureTransport
from app.normalization import (
    EventNormalizationError,
    PumpCompletionMigrationNormalizer,
    PumpCreateEventNormalizer,
    PumpTradeEventNormalizer,
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


def trade_fixture() -> ProviderEnvelope:
    return next(
        item.envelope
        for item in FixtureTransport([_FIXTURE_DIR]).load_all()
        if item.envelope.raw_payload.get("eventName") == "TradeEvent"
    )


def normalize_trade(fields: dict[str, object], *, event_index: int = 0):
    envelope = trade_fixture()
    event = DecodedEvent(
        event_name="TradeEvent",
        program_id=PUMP_ID,
        event_index=event_index,
        source_instruction_index=4,
        fields=fields,
        decode_status=DecodeStatus.DECODED,
    )
    return PumpTradeEventNormalizer(settings()).normalize(
        event,
        provider=envelope.provider,
        signature=envelope.signature,
        slot=envelope.slot,
        commitment=envelope.commitment,
        observed_at=envelope.received_at,
        raw_event_id=9,
    )


def test_fixture_decoder_and_trade_normalizer_match_trade_event_golden(db: Session) -> None:
    envelope = trade_fixture()
    raw = RawJournal(db).insert(envelope)
    decoded = FixtureDecoder(settings()).decode(
        DecoderInput(raw_event_id=raw.id, transport=raw.transport, payload_json=raw.payload_json)
    )
    assert decoded.status == DecodeStatus.DECODED
    assert len(decoded.events) == 1
    canonical = PumpTradeEventNormalizer(settings()).normalize(
        decoded.events[0],
        provider=envelope.provider,
        signature=envelope.signature,
        slot=envelope.slot,
        commitment=envelope.commitment,
        observed_at=envelope.received_at,
        raw_event_id=raw.id,
    )
    expected = json.loads(
        (
            _REPOSITORY_ROOT / "coding-pack" / "golden-tests" / "trade-event.expected.json"
        ).read_text()
    )
    projection = {
        "event_type": canonical.event_type.value,
        "program": canonical.program.value,
        "event_name": canonical.event_name,
        "mint": canonical.mint,
        "user": canonical.user,
        "decode_status": canonical.decode_status.value,
    }
    assert projection == expected
    assert canonical.raw_event_id == raw.id


def test_trade_fixture_buy_maps_golden_fields_and_preserves_raw_and_normalized_values() -> None:
    envelope = trade_fixture()
    fields = dict(envelope.raw_payload["decoded"])
    canonical = normalize_trade(fields)

    assert canonical.event_type == ScoutEventType.CURVE_BUY
    assert canonical.mint == fields["mint"]
    assert canonical.user == fields["user"]
    assert canonical.creator == fields["creator"]
    assert canonical.quote_mint == fields["quote_mint"]
    assert canonical.instruction_index == 4
    assert canonical.payload["sol_amount"] == "10000000"
    assert canonical.payload["quote_amount"] == "10000000"
    assert canonical.payload["virtual_quote_reserves"] == "0"
    assert canonical.amounts == {
        "quote_amount": "10000000",
        "token_amount": "35000000000",
        "fee": "100000",
        "fee_basis_points": "100",
        "creator_fee": "0",
        "creator_fee_bps": "0",
    }
    assert canonical.reserves == {
        "virtual_quote_reserves": "0",
        "virtual_token_reserves": "1072965000000000",
        "real_quote_reserves": "10000000",
        "real_token_reserves": "793065000000000",
    }


def test_trade_sell_maps_direction_without_inferring_quote_mint() -> None:
    envelope = trade_fixture()
    fields = dict(envelope.raw_payload["decoded"])
    fields["is_buy"] = False
    fields.pop("quote_mint")
    fields.pop("quote_amount", None)
    fields.pop("virtual_quote_reserves", None)
    fields.pop("real_quote_reserves", None)

    canonical = normalize_trade(fields, event_index=1)

    assert canonical.event_type == ScoutEventType.CURVE_SELL
    assert canonical.quote_mint is None
    assert canonical.payload["quote_amount"] == fields["sol_amount"]
    assert canonical.reserves["virtual_quote_reserves"] == fields["virtual_sol_reserves"]
    assert canonical.payload["sol_amount"] == fields["sol_amount"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("sol_amount", "-1"),
        ("token_amount", "18446744073709551616"),
        ("fee", True),
        ("is_buy", "true"),
        ("timestamp", 1 << 63),
        ("user", "not-an-address"),
    ],
    ids=(
        "negative-u64",
        "u64-overflow",
        "bool-as-amount",
        "non-bool-direction",
        "i64-overflow",
        "invalid-user",
    ),
)
def test_malformed_trade_fields_are_not_normalized(field: str, value: object) -> None:
    fields = dict(trade_fixture().raw_payload["decoded"])
    fields[field] = value

    with pytest.raises(EventNormalizationError, match="fields are malformed"):
        normalize_trade(fields)


def test_trade_preserves_future_appended_fields() -> None:
    fields = dict(trade_fixture().raw_payload["decoded"])
    fields["future_u64"] = "18446744073709551615"

    canonical = normalize_trade(fields)

    assert canonical.payload["future_u64"] == fields["future_u64"]


LIFECYCLE_MINT = "7LSsEoJGhLeZzGvDofTdNg7M3JttxQqGWNLo6vWMpump"
LIFECYCLE_USER = "9XDYTfQKwW8sHPqnFdUreMmtmffmkHVPGTNV2e3LKxNW"
LIFECYCLE_CURVE = "EsmVk4MTsoT71JFaRM5DWFZboKpMQjfY6EYzAgUuksXw"
LIFECYCLE_POOL = "5L5k7gtNLbeXdzpvNrFshg1E1id1ceUDfc6vPUTxp98q"
LIFECYCLE_QUOTE = "So11111111111111111111111111111111111111112"


def lifecycle_event(
    event_name: str, fields: dict[str, object], *, event_index: int = 0
) -> DecodedEvent:
    return DecodedEvent(
        event_name=event_name,
        program_id=PUMP_ID,
        event_index=event_index,
        source_instruction_index=8,
        fields=fields,
        decode_status=DecodeStatus.DECODED,
    )


def normalize_lifecycle(event: DecodedEvent, *, raw_event_id: int = 11):
    return PumpCompletionMigrationNormalizer(settings()).normalize(
        event,
        provider="helius",
        signature="1" * 64,
        slot=300000010,
        commitment=Commitment.CONFIRMED,
        observed_at=datetime(2026, 10, 13, 1, 1, tzinfo=UTC),
        raw_event_id=raw_event_id,
    )


def complete_fields() -> dict[str, object]:
    return {
        "user": LIFECYCLE_USER,
        "mint": LIFECYCLE_MINT,
        "bonding_curve": LIFECYCLE_CURVE,
        "timestamp": 1791853260,
        "quote_mint": LIFECYCLE_QUOTE,
    }


def post_complete_buy_fields() -> dict[str, object]:
    return {
        **complete_fields(),
        "base_out": "35000000000",
        "quote_in": "10000000",
        "fee_basis_points": "100",
        "fee": "100000",
        "creator_fee_basis_points": "0",
        "creator_fee": "0",
        "buyback_fee": "0",
        "pool_base_reserves_before": "800000000000000",
        "pool_quote_reserves_before": "10000000",
        "pool_base_reserves_after": "799965000000000",
        "pool_quote_reserves_after": "20000000",
    }


def migration_fields() -> dict[str, object]:
    return {
        **complete_fields(),
        "mint_amount": "793065000000000",
        "sol_amount": "85000000000",
        "pool_migration_fee": "1500000000",
        "pool": LIFECYCLE_POOL,
    }


def test_complete_event_maps_curve_completion_and_preserves_order_identity() -> None:
    canonical = normalize_lifecycle(
        lifecycle_event("CompleteEvent", complete_fields(), event_index=3)
    )

    assert canonical.event_type == ScoutEventType.CURVE_COMPLETE
    assert canonical.mint == LIFECYCLE_MINT
    assert canonical.user == LIFECYCLE_USER
    assert canonical.bonding_curve == LIFECYCLE_CURVE
    assert canonical.quote_mint == LIFECYCLE_QUOTE
    assert canonical.instruction_index == 8
    assert canonical.event_index == 3
    assert canonical.payload["curve_complete"] is True
    assert canonical.payload["event_time"] == 1791853260


def test_post_complete_buy_is_not_a_pre_completion_trade_and_retains_reserve_transition() -> None:
    canonical = normalize_lifecycle(
        lifecycle_event("PostCompleteBuyEvent", post_complete_buy_fields(), event_index=4)
    )

    assert canonical.event_type == ScoutEventType.CURVE_BUY
    assert canonical.payload["event_phase"] == "post_completion"
    assert canonical.amounts == {
        "base_amount": "35000000000",
        "quote_amount": "10000000",
        "fee": "100000",
        "creator_fee": "0",
        "buyback_fee": "0",
    }
    assert canonical.reserves["pool_base_reserves_before"] == "800000000000000"
    assert canonical.reserves["pool_base_reserves_after"] == "799965000000000"
    assert canonical.payload["base_out"] == "35000000000"


def test_migration_links_pool_immediately_but_keeps_linkage_pending_until_pool_event() -> None:
    canonical = normalize_lifecycle(
        lifecycle_event("CompletePumpAmmMigrationEvent", migration_fields(), event_index=5)
    )

    assert canonical.event_type == ScoutEventType.MIGRATION
    assert canonical.pool == LIFECYCLE_POOL
    assert canonical.bonding_curve == LIFECYCLE_CURVE
    assert canonical.payload["migrated"] is True
    assert canonical.payload["pool_linkage_status"] == "pending"
    assert canonical.amounts == {
        "mint_amount": "793065000000000",
        "sol_amount": "85000000000",
        "pool_migration_fee": "1500000000",
    }


def test_lifecycle_normalization_is_idempotent_and_event_order_is_preserved() -> None:
    first = normalize_lifecycle(lifecycle_event("CompleteEvent", complete_fields(), event_index=6))
    repeated = normalize_lifecycle(
        lifecycle_event("CompleteEvent", complete_fields(), event_index=6)
    )
    later = normalize_lifecycle(
        lifecycle_event("CompletePumpAmmMigrationEvent", migration_fields(), event_index=7)
    )

    assert first.event_key == repeated.event_key
    assert first.model_dump(mode="json") == repeated.model_dump(mode="json")
    assert first.event_index < later.event_index
    assert first.slot == later.slot


@pytest.mark.parametrize(
    "event_name,mutation",
    [
        ("CompleteEvent", lambda fields: fields.pop("quote_mint")),
        (
            "PostCompleteBuyEvent",
            lambda fields: fields.__setitem__("base_out", "18446744073709551616"),
        ),
        ("CompletePumpAmmMigrationEvent", lambda fields: fields.__setitem__("pool", "not-address")),
        ("CompletePumpAmmMigrationEvent", lambda fields: fields.__setitem__("mint_amount", True)),
    ],
    ids=("missing-required", "u64-overflow", "invalid-pool", "bool-as-u64"),
)
def test_malformed_lifecycle_fields_are_not_normalized(event_name: str, mutation) -> None:
    fields = {
        "CompleteEvent": complete_fields,
        "PostCompleteBuyEvent": post_complete_buy_fields,
        "CompletePumpAmmMigrationEvent": migration_fields,
    }[event_name]()
    mutation(fields)

    with pytest.raises(EventNormalizationError, match="lifecycle event fields are malformed"):
        normalize_lifecycle(lifecycle_event(event_name, fields))


def test_unknown_lifecycle_event_remains_unsupported() -> None:
    event = lifecycle_event("FutureLifecycleEvent", complete_fields())

    with pytest.raises(UnsupportedNormalizationError):
        normalize_lifecycle(event)
