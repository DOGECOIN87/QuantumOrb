"""Offline lifecycle replay, observation uncertainty, and transaction tests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import permutations
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.canonical_events import CanonicalEventRepository, ScoutEventRecord
from app.db.lifecycles import (
    LifecycleContextUnavailable,
    LifecycleError,
    LifecycleRepository,
    project_lifecycle,
)
from app.db.models import raw_events, scout_events, token_lifecycles, tokens
from app.db.session import create_database_engine
from app.domain.enums import Commitment, ScoutEventType
from app.domain.keys import canonical_event_key
from app.domain.models import CanonicalSolanaEvent, JsonObject

MINT = "1" * 32
OTHER_MINT = "1" * 31 + "2"
CURVE = "1" * 31 + "3"
POOL = "1" * 31 + "4"
CREATOR = "1" * 31 + "5"
LAUNCH_USER = "1" * 31 + "6"
TRADER = "1" * 31 + "7"
QUOTE_MINT = "1" * 31 + "8"
SIGNATURE = "1" * 64
OTHER_SIGNATURE = "1" * 63 + "2"
NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)
EVENT_NAMES = {
    "token_created": "CreateEvent",
    "curve_buy": "TradeEvent",
    "curve_sell": "TradeEvent",
    "curve_complete": "CompleteEvent",
    "migration": "CompletePumpAmmMigrationEvent",
    "pool_created": "CreatePoolEvent",
    "pool_buy": "BuyEvent",
    "pool_sell": "SellEvent",
}


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    database = create_database_engine(f"sqlite:///{tmp_path / 'lifecycles.db'}")
    Base.metadata.create_all(database)
    with Session(database) as session:
        payload = json.dumps({"fixture": "lifecycle"}, sort_keys=True)
        session.execute(
            insert(raw_events).values(
                id=1,
                event_key="0" * 64,
                provider="helius",
                transport="fixture",
                notification_id="lifecycle-fixture",
                signature=SIGNATURE,
                slot=0,
                commitment="confirmed",
                received_at=NOW,
                payload_json=payload,
                payload_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                status="new",
                created_at=NOW,
            )
        )
        session.commit()
    yield database
    database.dispose()


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    with Session(engine, expire_on_commit=False) as session:
        yield session


def make_event(
    event_type: str,
    *,
    slot: int = 0,
    event_index: int = 0,
    transaction_index: int | None = 0,
    instruction_index: int | None = 0,
    signature: str = SIGNATURE,
    mint: str | None = MINT,
    commitment: Commitment = Commitment.CONFIRMED,
    event_name: str | None = None,
    payload: JsonObject | None = None,
    creator: str | None = CREATOR,
    user: str | None = LAUNCH_USER,
    pool: str | None = None,
) -> CanonicalSolanaEvent:
    program = "pump_amm" if event_type.startswith("pool_") else "pump"
    program_id = f"{program}-fixture-program"
    name = event_name or EVENT_NAMES[event_type]
    finalized = commitment == Commitment.FINALIZED
    return CanonicalSolanaEvent(
        event_key=canonical_event_key("helius", signature, program_id, name, event_index),
        signature=signature,
        slot=slot,
        observed_at=NOW + timedelta(seconds=event_index),
        commitment=commitment,
        program=program,
        program_id=program_id,
        event_name=name,
        event_type=ScoutEventType(event_type),
        mint=mint,
        creator=creator,
        user=user,
        bonding_curve=CURVE if program == "pump" else None,
        pool=pool or (POOL if event_type == "migration" or program == "pump_amm" else None),
        quote_mint=QUOTE_MINT,
        transaction_index=transaction_index,
        instruction_index=instruction_index,
        event_index=event_index,
        payload=payload or {},
        decode_status="decoded",
        finality_status="finalized" if finalized else "provisional",
        reconciliation_status="not_required" if finalized else "pending",
        derivation_version="t015-fixture",
        raw_event_id=1,
    )


def persist(db: Session, event: CanonicalSolanaEvent) -> ScoutEventRecord:
    return CanonicalEventRepository(db).insert_if_absent(event)


def count(engine: Engine, table: object) -> int:
    with Session(engine) as session:
        return session.execute(select(func.count()).select_from(table)).scalar_one()


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        ("token_created", "seen_create"),
        ("curve_buy", "curve_trading"),
        ("curve_sell", "curve_trading"),
        ("curve_complete", "curve_complete"),
        ("migration", "migration_seen"),
        ("pool_created", "pool_active"),
        ("pool_buy", "pool_trading"),
        ("pool_sell", "pool_trading"),
    ],
)
def test_each_supported_event_establishes_its_observed_phase(
    db: Session, phase: str, expected: str
) -> None:
    event = persist(db, make_event(phase))
    result = LifecycleRepository(db).apply(event)

    assert result.mint == MINT
    assert result.state == expected
    assert result.first_seen_slot == result.last_seen_slot == 0
    assert result.last_event_id == event.id
    assert result.provisional is True
    assert result.data_gap is (phase != "token_created")
    assert result.updated_at == NOW


def test_complete_sequence_preserves_launch_identity_and_pool_context(db: Session) -> None:
    repository = LifecycleRepository(db)
    phases = [
        ("token_created", "seen_create"),
        ("curve_buy", "curve_trading"),
        ("curve_sell", "curve_trading"),
        ("curve_complete", "curve_complete"),
        ("migration", "migration_seen"),
        ("pool_created", "pool_active"),
        ("pool_buy", "pool_trading"),
        ("pool_sell", "pool_trading"),
    ]
    for index, (phase, expected) in enumerate(phases):
        event = persist(
            db,
            make_event(
                phase,
                slot=index,
                event_index=index,
                user=LAUNCH_USER if index == 0 else TRADER,
                creator=CREATOR if index == 0 else OTHER_MINT,
                mint=None if phase in {"pool_buy", "pool_sell"} else MINT,
            ),
        )
        result = repository.apply(event)
        assert result.state == expected
        assert result.data_gap is False

    assert result.creator == CREATOR
    assert result.user == LAUNCH_USER
    assert result.bonding_curve == CURVE
    assert result.pool == POOL
    assert result.quote_mint == QUOTE_MINT
    assert result.first_seen_slot == 0
    assert result.last_seen_slot == len(phases) - 1
    assert result.updated_at == NOW + timedelta(seconds=len(phases) - 1)
    placeholder = db.execute(select(tokens).where(tokens.c.mint == MINT)).one()._mapping
    assert placeholder["metadata_status"] == "unknown"
    assert placeholder["supply_raw"] is None
    assert placeholder["decimals"] is None


@pytest.mark.parametrize("order", list(permutations(range(4))))
def test_every_arrival_permutation_replays_to_same_phase_and_repairs_gaps(
    db: Session, order: tuple[int, ...]
) -> None:
    phases = ["token_created", "curve_buy", "curve_complete", "migration"]
    repository = LifecycleRepository(db)
    for index in order:
        event = persist(db, make_event(phases[index], slot=index, event_index=index))
        result = repository.apply(event)

    assert result.state == "migration_seen"
    assert result.data_gap is False
    assert result.first_seen_slot == 0
    assert result.last_seen_slot == 3
    assert result.creator == CREATOR
    assert result.user == LAUNCH_USER
    assert db.scalar(select(func.count()).select_from(token_lifecycles)) == 1


def test_duplicates_leave_projection_and_placeholder_unchanged(db: Session) -> None:
    canonical = make_event("token_created")
    event = persist(db, canonical)
    repository = LifecycleRepository(db)
    first = repository.apply(event)
    duplicate = persist(db, canonical)
    second = repository.apply(duplicate)
    third = repository.apply(event)

    assert first == second == third
    assert db.scalar(select(func.count()).select_from(scout_events)) == 1
    assert db.scalar(select(func.count()).select_from(token_lifecycles)) == 1
    assert db.scalar(select(func.count()).select_from(tokens)) == 1


def test_late_create_repairs_discovery_gap_without_reversing_trade_state(db: Session) -> None:
    trade = persist(db, make_event("curve_buy", slot=20, event_index=1, user=TRADER))
    repository = LifecycleRepository(db)
    first = repository.apply(trade)
    assert first.state == "curve_trading"
    assert first.data_gap is True
    assert first.user is None

    creation = persist(db, make_event("token_created", slot=10))
    repaired = repository.apply(creation)
    assert repaired.state == "curve_trading"
    assert repaired.data_gap is False
    assert repaired.first_seen_slot == 10
    assert repaired.last_seen_slot == 20
    assert repaired.last_event_id == trade.id
    assert repaired.user == LAUNCH_USER


def test_newer_curve_trade_cannot_reverse_pool_phase(db: Session) -> None:
    pool = persist(db, make_event("pool_created", slot=10))
    repository = LifecycleRepository(db)
    assert repository.apply(pool).state == "pool_active"
    trade = persist(db, make_event("curve_sell", slot=20, event_index=1))
    result = repository.apply(trade)
    assert result.state == "pool_active"
    assert result.last_event_id == trade.id
    assert result.last_seen_slot == 20
    assert result.data_gap is True


def test_post_completion_buy_proves_completion_without_separate_complete_event(db: Session) -> None:
    persist(db, make_event("token_created"))
    buy = persist(
        db,
        make_event(
            "curve_buy",
            slot=1,
            event_index=1,
            event_name="PostCompleteBuyEvent",
            payload={"event_phase": "post_completion"},
            user=TRADER,
        ),
    )
    result = LifecycleRepository(db).apply(buy)
    assert result.state == "curve_complete"
    assert result.data_gap is False

    migration = persist(db, make_event("migration", slot=2, event_index=2))
    result = LifecycleRepository(db).apply(migration)
    assert result.state == "migration_seen"
    assert result.data_gap is False


@pytest.mark.parametrize("known_dimension", ["transaction", "instruction", "event"])
def test_zero_indices_are_real_positions_and_out_of_order_apply_uses_chain_order(
    db: Session, known_dimension: str
) -> None:
    creation = make_event("token_created", slot=5, event_index=0)
    trade = make_event(
        "curve_buy",
        slot=5,
        event_index=1 if known_dimension == "event" else 0,
        signature=OTHER_SIGNATURE if known_dimension == "transaction" else SIGNATURE,
        transaction_index=1 if known_dimension == "transaction" else 0,
        instruction_index=1 if known_dimension == "instruction" else 0,
    )
    trade_record = persist(db, trade)
    creation_record = persist(db, creation)
    result = LifecycleRepository(db).apply(creation_record)
    assert result.state == "curve_trading"
    assert result.data_gap is False
    assert result.last_event_id == trade_record.id


@pytest.mark.parametrize(
    ("signature", "transaction_index", "instruction_index"),
    [
        (OTHER_SIGNATURE, None, 0),
        (OTHER_SIGNATURE, 0, 0),
        (SIGNATURE, 1, 0),
        (SIGNATURE, 0, None),
    ],
)
def test_same_slot_uncertain_or_conflicting_positions_carry_data_gap(
    db: Session, signature: str, transaction_index: int | None, instruction_index: int | None
) -> None:
    persist(db, make_event("token_created", slot=5))
    trade = persist(
        db,
        make_event(
            "curve_buy",
            slot=5,
            event_index=1,
            signature=signature,
            transaction_index=transaction_index,
            instruction_index=instruction_index,
        ),
    )
    result = LifecycleRepository(db).apply(trade)
    assert result.state == "curve_trading"
    assert result.data_gap is True


def test_ambiguous_order_is_deterministic_for_opposite_input_order(db: Session) -> None:
    first = persist(db, make_event("token_created", slot=5, transaction_index=None))
    second = persist(
        db,
        make_event("curve_buy", slot=5, event_index=1, signature=OTHER_SIGNATURE),
    )
    assert project_lifecycle(MINT, [first, second]) == project_lifecycle(MINT, [second, first])


def test_conflicting_event_positions_in_one_transaction_are_flagged(db: Session) -> None:
    persist(db, make_event("token_created", slot=5))
    trade = persist(db, make_event("curve_buy", slot=5))
    result = LifecycleRepository(db).apply(trade)
    assert result.state == "curve_trading"
    assert result.data_gap is True


def test_authoritative_finality_refreshes_a_stale_snapshot(db: Session) -> None:
    canonical = make_event("token_created")
    original = persist(db, canonical)
    repository = LifecycleRepository(db)
    assert repository.apply(original).provisional is True
    CanonicalEventRepository(db).reconcile_finalized(
        canonical.model_copy(update={"commitment": Commitment.FINALIZED})
    )
    result = repository.apply(original)
    assert original.finality_status == "provisional"
    assert result.provisional is False
    assert result.data_gap is False
    assert result.last_event_id == original.id


def test_finalizing_latest_event_does_not_hide_provisional_creation(db: Session) -> None:
    creation = make_event("token_created")
    original = persist(db, creation)
    trade = persist(
        db,
        make_event("curve_buy", slot=1, event_index=1, commitment=Commitment.FINALIZED),
    )
    repository = LifecycleRepository(db)
    assert repository.apply(trade).provisional is True
    CanonicalEventRepository(db).reconcile_finalized(
        creation.model_copy(update={"commitment": Commitment.FINALIZED})
    )
    result = repository.apply(original)
    assert result.provisional is False
    assert result.state == "curve_trading"


def test_changed_finalized_event_keeps_initial_evidence_and_marks_uncertainty(db: Session) -> None:
    canonical = make_event("token_created", payload={"name": "original"})
    original = persist(db, canonical)
    LifecycleRepository(db).apply(original)
    CanonicalEventRepository(db).reconcile_finalized(
        canonical.model_copy(
            update={"commitment": Commitment.FINALIZED, "payload": {"name": "changed"}}
        )
    )
    result = LifecycleRepository(db).apply(original)
    assert result.state == "seen_create"
    assert result.provisional is True
    assert result.data_gap is True
    assert db.scalar(select(scout_events.c.payload_json)) == '{"name":"original"}'


def test_missing_reconciliation_is_visible_even_with_finalized_status(db: Session) -> None:
    original = persist(db, make_event("token_created", commitment=Commitment.FINALIZED))
    db.execute(
        update(scout_events)
        .where(scout_events.c.id == original.id)
        .values(reconciliation_status="missing")
    )
    db.commit()
    result = LifecycleRepository(db).apply(original)
    assert result.data_gap is True
    assert result.provisional is True


def test_pool_trade_remains_canonical_until_observed_pool_context_arrives(db: Session) -> None:
    trade = persist(db, make_event("pool_buy", slot=20, event_index=1, mint=None))
    repository = LifecycleRepository(db)
    with pytest.raises(LifecycleContextUnavailable, match="unique observed mint"):
        repository.apply(trade)
    assert db.scalar(select(func.count()).select_from(scout_events)) == 1
    assert db.scalar(select(func.count()).select_from(tokens)) == 0
    assert db.scalar(select(func.count()).select_from(token_lifecycles)) == 0

    pool = persist(db, make_event("pool_created", slot=10))
    result = repository.apply(pool)
    assert result.state == "pool_trading"
    assert result.last_event_id == trade.id
    assert result.pool == POOL
    assert db.scalar(select(scout_events.c.mint).where(scout_events.c.id == trade.id)) is None


def test_trade_participant_cannot_supply_pool_to_mint_mapping(db: Session) -> None:
    persist(db, make_event("pool_buy", mint=MINT))
    unresolved = persist(db, make_event("pool_sell", event_index=1, mint=None))
    with pytest.raises(LifecycleContextUnavailable):
        LifecycleRepository(db).apply(unresolved)


def test_migration_context_links_pool_trade_without_inventing_pool_creation(db: Session) -> None:
    migration = persist(db, make_event("migration", slot=10))
    trade = persist(db, make_event("pool_sell", slot=20, event_index=1, mint=None))
    result = LifecycleRepository(db).apply(trade)
    assert result.state == "pool_trading"
    assert result.mint == migration.mint
    assert result.data_gap is True


def test_conflicting_pool_context_does_not_guess_trade_mint(db: Session) -> None:
    first = persist(db, make_event("pool_created", slot=10))
    persist(db, make_event("pool_created", slot=11, event_index=1, mint=OTHER_MINT))
    trade = persist(db, make_event("pool_buy", slot=20, event_index=2, mint=None))
    repository = LifecycleRepository(db)
    with pytest.raises(LifecycleContextUnavailable):
        repository.apply(trade)
    result = repository.apply(first)
    assert result.mint == MINT
    assert result.state == "pool_active"
    assert result.data_gap is True
    assert result.last_event_id == first.id


def test_new_pool_conflict_cannot_reverse_previously_observed_pool_trading(db: Session) -> None:
    first = persist(db, make_event("pool_created", slot=10))
    trade = persist(db, make_event("pool_buy", slot=20, event_index=1, mint=None))
    repository = LifecycleRepository(db)
    assert repository.apply(trade).state == "pool_trading"
    persist(db, make_event("pool_created", slot=30, event_index=2, mint=OTHER_MINT))
    result = repository.apply(first)
    assert result.state == "pool_trading"
    assert result.provisional is True
    assert result.data_gap is True


def test_changed_pool_mapping_cannot_resolve_new_trades_or_reverse_prior_phase(db: Session) -> None:
    canonical = make_event("pool_created", slot=10)
    pool = persist(db, canonical)
    trade = persist(db, make_event("pool_buy", slot=20, event_index=1, mint=None))
    repository = LifecycleRepository(db)
    assert repository.apply(trade).state == "pool_trading"
    CanonicalEventRepository(db).reconcile_finalized(
        canonical.model_copy(
            update={"commitment": Commitment.FINALIZED, "payload": {"changed": True}}
        )
    )
    with pytest.raises(LifecycleContextUnavailable):
        repository.apply(trade)
    result = repository.apply(pool)
    assert result.state == "pool_trading"
    assert result.provisional is True
    assert result.data_gap is True


@pytest.mark.parametrize(
    ("finality_status", "reconciliation_status"),
    [
        ("finalized", "missing"),
        ("finalized", "changed"),
        ("reorged_or_changed", "pending"),
    ],
)
def test_disputed_pool_mappings_cannot_associate_unknown_mint_trades(
    db: Session, finality_status: str, reconciliation_status: str
) -> None:
    pool = persist(db, make_event("pool_created"))
    db.execute(
        update(scout_events)
        .where(scout_events.c.id == pool.id)
        .values(finality_status=finality_status, reconciliation_status=reconciliation_status)
    )
    db.commit()
    trade = persist(db, make_event("pool_sell", slot=1, event_index=1, mint=None))
    repository = LifecycleRepository(db)
    with pytest.raises(LifecycleContextUnavailable):
        repository.apply(trade)
    result = repository.apply(pool)
    assert result.state == "pool_active"
    assert result.data_gap is True
    assert result.provisional is True
    assert result.last_event_id == pool.id


def test_unknown_mint_non_trade_does_not_create_guessed_identity(db: Session) -> None:
    event = persist(db, make_event("token_created", mint=None))
    with pytest.raises(LifecycleContextUnavailable):
        LifecycleRepository(db).apply(event)
    assert db.scalar(select(func.count()).select_from(tokens)) == 0


def test_unknown_mint_trade_without_pool_remains_unresolved(db: Session) -> None:
    canonical = make_event("pool_buy", mint=None).model_copy(update={"pool": None})
    event = persist(db, canonical)
    with pytest.raises(LifecycleContextUnavailable):
        LifecycleRepository(db).apply(event)
    assert db.scalar(select(func.count()).select_from(tokens)) == 0


def test_pool_only_discovery_uses_creator_without_promoting_trader_to_launch_user(
    db: Session,
) -> None:
    pool = persist(db, make_event("pool_created", creator=CREATOR, user=TRADER))
    result = LifecycleRepository(db).apply(pool)
    assert result.creator == CREATOR
    assert result.user is None


@pytest.mark.parametrize("mint", ["", "invalid", "0" * 32, "1" * 31, "1" * 33])
def test_invalid_mint_is_rejected_before_creating_token_or_lifecycle(
    db: Session, mint: str
) -> None:
    event = persist(db, make_event("token_created", mint=mint))
    with pytest.raises(LifecycleError, match="mint is invalid"):
        LifecycleRepository(db).apply(event)
    assert db.scalar(select(func.count()).select_from(tokens)) == 0
    assert db.scalar(select(func.count()).select_from(token_lifecycles)) == 0


def test_unpersisted_or_mismatched_identity_is_rejected(db: Session) -> None:
    stored = persist(db, make_event("token_created"))
    for invalid in (replace(stored, id=999), replace(stored, event_key="f" * 64)):
        with pytest.raises(LifecycleError, match="persisted first"):
            LifecycleRepository(db).apply(invalid)
    assert db.scalar(select(func.count()).select_from(tokens)) == 0


def test_stale_caller_metadata_is_not_used_in_place_of_authoritative_row(db: Session) -> None:
    stored = persist(db, make_event("token_created"))
    result = LifecycleRepository(db).apply(replace(stored, mint=OTHER_MINT, creator=TRADER))
    assert result.mint == MINT
    assert result.creator == CREATOR


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"decode_status": "raw_only"}, "supported decoded events"),
        ({"decode_status": "failed"}, "supported decoded events"),
        ({"event_type": "unknown"}, "supported decoded events"),
        ({"mint": OTHER_MINT}, "does not match"),
        ({"slot": -1}, "ordering metadata"),
        ({"transaction_index": -1}, "ordering metadata"),
        ({"instruction_index": -1}, "ordering metadata"),
        ({"event_index": -1}, "ordering metadata"),
        ({"slot": True}, "ordering metadata"),
        ({"observed_at": NOW.replace(tzinfo=None)}, "timezone-aware"),
    ],
)
def test_projection_rejects_invalid_observation_metadata(
    db: Session, changes: dict[str, object], message: str
) -> None:
    stored = persist(db, make_event("token_created"))
    with pytest.raises(LifecycleError, match=message):
        project_lifecycle(MINT, [replace(stored, **changes)])


def test_empty_history_cannot_create_a_lifecycle() -> None:
    with pytest.raises(LifecycleError, match="requires stored decoded events"):
        project_lifecycle(MINT, [])


@pytest.mark.parametrize("decode_status", ["failed", "raw_only"])
def test_repository_rejects_stored_non_decoded_events(db: Session, decode_status: str) -> None:
    event = persist(db, make_event("token_created"))
    db.execute(
        update(scout_events)
        .where(scout_events.c.id == event.id)
        .values(decode_status=decode_status)
    )
    db.commit()
    with pytest.raises(LifecycleError, match="supported decoded events"):
        LifecycleRepository(db).apply(event)
    assert db.scalar(select(func.count()).select_from(tokens)) == 0


@pytest.mark.parametrize("state", ["error", "curve_active"])
def test_replay_preserves_existing_error_or_intermediate_phase_with_flags(
    db: Session, state: str
) -> None:
    event = persist(db, make_event("token_created"))
    repository = LifecycleRepository(db)
    repository.apply(event)
    db.execute(update(token_lifecycles).values(state=state))
    db.commit()
    result = repository.apply(event)
    assert result.state == state
    assert result.data_gap is True
    assert result.provisional is True


def test_corrupt_stored_phase_cannot_be_silently_rewritten(db: Session) -> None:
    event = persist(db, make_event("token_created"))
    repository = LifecycleRepository(db)
    repository.apply(event)
    db.execute(update(token_lifecycles).values(state="unknown"))
    db.commit()
    with pytest.raises(LifecycleError, match="stored lifecycle phase is unsupported"):
        repository.apply(event)
    assert db.scalar(select(token_lifecycles.c.state)) == "unknown"


def test_existing_token_enrichment_is_preserved_by_lifecycle_replay(db: Session) -> None:
    event = persist(db, make_event("token_created"))
    db.execute(
        insert(tokens).values(
            mint=MINT,
            symbol="KNOWN",
            decimals=6,
            supply_raw="1000000",
            first_seen_at=NOW - timedelta(days=1),
            last_seen_at=NOW,
            metadata_status="complete",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    db.commit()
    LifecycleRepository(db).apply(event)
    token = db.execute(select(tokens)).one()._mapping
    assert token["metadata_status"] == "complete"
    assert token["symbol"] == "KNOWN"
    assert token["decimals"] == 6
    assert token["supply_raw"] == "1000000"


def test_caller_session_owns_commit_and_rolls_back_entire_projection(
    db: Session, engine: Engine
) -> None:
    stored = persist(db, make_event("token_created"))
    db.rollback()
    with Session(engine) as caller:
        with pytest.raises(RuntimeError, match="abort caller"):
            with caller.begin():
                LifecycleRepository(caller).apply(stored)
                assert caller.scalar(select(func.count()).select_from(tokens)) == 1
                assert caller.scalar(select(func.count()).select_from(token_lifecycles)) == 1
                raise RuntimeError("abort caller")
        assert not caller.in_transaction()
    assert count(engine, tokens) == 0
    assert count(engine, token_lifecycles) == 0
    assert count(engine, scout_events) == 1


def test_factory_created_session_commits_and_closes_on_success(db: Session, engine: Engine) -> None:
    stored = persist(db, make_event("token_created"))
    db.rollback()
    sessions: list[TrackingSession] = []

    class TrackingSession(Session):
        closed = False

        def close(self) -> None:
            self.closed = True
            super().close()

    def factory() -> Session:
        session = TrackingSession(engine)
        sessions.append(session)
        return session

    result = LifecycleRepository(factory).apply(stored)
    assert result.state == "seen_create"
    assert len(sessions) == 1 and sessions[0].closed
    assert count(engine, tokens) == 1
    assert count(engine, token_lifecycles) == 1


def test_factory_created_session_closes_after_validation_failure(
    db: Session, engine: Engine
) -> None:
    stored = persist(db, make_event("token_created", mint="invalid"))
    db.rollback()
    sessions: list[TrackingSession] = []

    class TrackingSession(Session):
        closed = False

        def close(self) -> None:
            self.closed = True
            super().close()

    def factory() -> Session:
        session = TrackingSession(engine)
        sessions.append(session)
        return session

    with pytest.raises(LifecycleError, match="mint is invalid"):
        LifecycleRepository(factory).apply(stored)
    assert len(sessions) == 1 and sessions[0].closed
    assert count(engine, tokens) == 0
    assert count(engine, token_lifecycles) == 0


def install_insert_failure(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TRIGGER reject_lifecycle BEFORE INSERT ON token_lifecycles "
                "BEGIN SELECT RAISE(ABORT, 'fixture lifecycle write failure'); END"
            )
        )


def test_sql_failure_rolls_back_factory_token_placeholder(db: Session, engine: Engine) -> None:
    stored = persist(db, make_event("token_created"))
    db.rollback()
    install_insert_failure(engine)
    repository = LifecycleRepository(sessionmaker(engine))
    with pytest.raises(IntegrityError, match="fixture lifecycle write failure"):
        repository.apply(stored)
    assert count(engine, tokens) == 0
    assert count(engine, token_lifecycles) == 0
    assert count(engine, scout_events) == 1


def test_sql_failure_rolls_back_savepoint_and_preserves_caller_work(
    db: Session, engine: Engine
) -> None:
    stored = persist(db, make_event("token_created"))
    db.rollback()
    install_insert_failure(engine)
    with Session(engine) as caller, caller.begin():
        caller.execute(
            insert(tokens).values(
                mint=OTHER_MINT,
                first_seen_at=NOW,
                last_seen_at=NOW,
                metadata_status="unknown",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        with pytest.raises(IntegrityError, match="fixture lifecycle write failure"):
            LifecycleRepository(caller).apply(stored)
        assert caller.scalar(select(tokens.c.mint)) == OTHER_MINT
        assert caller.scalar(select(func.count()).select_from(token_lifecycles)) == 0
    assert count(engine, tokens) == 1
    assert count(engine, token_lifecycles) == 0


def test_sql_update_failure_preserves_the_previous_projection(db: Session, engine: Engine) -> None:
    creation = persist(db, make_event("token_created"))
    factory = sessionmaker(engine)
    previous = LifecycleRepository(factory).apply(creation)
    trade = persist(db, make_event("curve_buy", slot=1, event_index=1))
    db.rollback()
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TRIGGER reject_lifecycle_update BEFORE UPDATE ON token_lifecycles "
                "BEGIN SELECT RAISE(ABORT, 'fixture lifecycle update failure'); END"
            )
        )
    with pytest.raises(IntegrityError, match="fixture lifecycle update failure"):
        LifecycleRepository(factory).apply(trade)
    with Session(engine) as observer:
        stored = observer.execute(select(token_lifecycles)).one()._mapping
        assert stored["state"] == previous.state
        assert stored["last_event_id"] == previous.last_event_id
        assert stored["last_seen_slot"] == previous.last_seen_slot
    assert count(engine, scout_events) == 2


def test_foreign_keys_reject_lifecycle_without_parent_token(engine: Engine) -> None:
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA foreign_keys")) == 1
    with Session(engine) as session, pytest.raises(IntegrityError, match="FOREIGN KEY"):
        session.execute(
            insert(token_lifecycles).values(
                mint=MINT,
                state="seen_create",
                provisional=True,
                data_gap=False,
                updated_at=NOW,
            )
        )
