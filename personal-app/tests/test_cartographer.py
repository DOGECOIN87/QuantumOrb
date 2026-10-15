"""End-to-end summary, uncertainty, durability, bounded scope, and rollback."""

import asyncio
import json
from pathlib import Path

import pytest
from cartographer_helpers import MINT, NOW, A, B, C, activity, history, transfer
from jsonschema import Draft202012Validator
from sqlalchemy import select

from app.cartographer import CartographerError, CartographerService
from app.config import Settings
from app.db.graph import GraphRepository
from app.db.models import graph_edge_evidence, job_runs
from app.db.session import create_all, create_database_engine, create_session_factory
from app.temporal import TemporalDetector


@pytest.fixture
def factory(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'summary.db'}")
    create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def setup_scope(factory, *, gap=1):
    activity(factory, wallet=B)
    activity(factory, wallet=C, number=91)
    return {
        B: history(factory, transfer()),
        C: history(factory, transfer(recipient=C, age=120 + gap, number=2), wallet=C),
    }


def service(factory, **settings):
    return CartographerService(factory, Settings(_env_file=None, **settings))


def test_summary_matches_full_golden_and_message_contract(factory):
    result = service(factory).analyze(MINT, setup_scope(factory), observed_at=NOW)
    actual = result.summary.model_dump(mode="json", exclude={"run_id", "observed_at"})
    root = Path(__file__).parents[2] / "coding-pack"
    golden = json.loads((root / "golden-tests/cartographer-summary.expected.json").read_text())
    assert actual == golden
    schema = json.loads((root / "schemas/cartographer-evidence.schema.json").read_text())
    Draft202012Validator(schema).validate(result.message.model_dump(mode="json"))
    assert result.message.nodes_added == 4 and result.message.edges_added == 5
    assert result.message.summary_ref == result.message.run_id
    assert service(factory).get_summary(run_id=result.summary.run_id) == result.summary
    assert service(factory).get_summary(mint=MINT) == result.summary


def test_replay_creates_independent_run_without_duplicate_evidence(factory):
    histories = setup_scope(factory)
    first = service(factory).analyze(MINT, histories, observed_at=NOW)
    with factory() as session:
        before = session.execute(select(graph_edge_evidence)).all()
    second = service(factory).analyze(MINT, histories, observed_at=NOW)
    assert first.summary.run_id != second.summary.run_id
    assert second.message.nodes_added == second.message.edges_added == 0
    with factory() as session:
        assert session.execute(select(graph_edge_evidence)).all() == before


def test_missing_history_preserves_known_counts_and_uncertainty(factory):
    histories = setup_scope(factory)
    result = service(factory).analyze(MINT, {B: histories[B]}, observed_at=NOW).summary
    assert not result.coverage.history_complete
    assert result.coverage.missing_wallet_histories == 1
    assert result.wallets_with_inbound_funding == 1
    assert "wallet_history_not_read" in result.warnings
    assert result.same_bundle_groups is None


def test_all_history_missing_is_unknown_instead_of_zero_funding(factory):
    activity(factory)
    result = service(factory).analyze(MINT, observed_at=NOW).summary
    assert result.wallets_with_inbound_funding is None and result.shared_funder_groups is None
    assert not result.coverage.history_complete


def test_no_canonical_activity_remains_not_read(factory):
    result = service(factory).analyze(MINT, observed_at=NOW).summary
    assert result.state.value == "not_read"
    assert result.trader_count_observed is None and result.same_slot_groups is None
    assert result.same_bundle_groups is None


def test_shared_funder_requires_configured_short_time_window(factory):
    result = service(factory).analyze(MINT, setup_scope(factory, gap=10), observed_at=NOW).summary
    assert result.wallets_with_inbound_funding == 2 and result.shared_funder_groups == 0


def test_create_user_and_declared_creator_stay_distinct(factory):
    activity(factory, wallet=B, creator=A, event_type="token_created")
    result = service(factory).analyze(MINT, observed_at=NOW).summary
    assert result.creator_wallet == A and result.user_wallet == B
    assert {edge.edge_type.value for edge in GraphRepository(factory).edges()} >= {
        "creator_declared_for_token",
        "user_created_token",
    }


@pytest.mark.parametrize(
    "setting,warning",
    [
        ({"cartographer_max_wallets_per_token": 1}, "wallet_limit_reached"),
        ({"cartographer_max_traders_per_token": 1}, "trader_limit_reached"),
        ({"cartographer_max_history_tx": 1}, "scout_event_limit_reached"),
    ],
)
def test_limits_are_persisted_and_warned(factory, setting, warning):
    histories = setup_scope(factory)
    if "cartographer_max_wallets_per_token" in setting:
        histories = {B: histories[B]}
    result = service(factory, **setting).analyze(MINT, histories, observed_at=NOW).summary
    assert warning in result.warnings and not result.coverage.history_complete
    assert (
        result.coverage.limits[
            {
                "wallet_limit_reached": "max_wallets",
                "trader_limit_reached": "max_traders",
                "scout_event_limit_reached": "max_history_transactions",
            }[warning]
        ]
        == 1
    )


def test_failed_analysis_rolls_back_graph_and_records_safe_failure(factory, monkeypatch):
    histories = setup_scope(factory)

    def fail(*args, **kwargs):
        raise RuntimeError("provider details must never appear in the audit")

    monkeypatch.setattr(TemporalDetector, "analyze", fail)
    with pytest.raises(CartographerError):
        service(factory).analyze(MINT, histories, observed_at=NOW)
    assert not GraphRepository(factory).nodes()
    with factory() as session:
        rows = (
            session.execute(select(job_runs).where(job_runs.c.job_name == "cartographer"))
            .mappings()
            .all()
        )
    assert len(rows) == 1 and rows[0]["status"] == "failed"
    assert rows[0]["safe_error_message"] == "Cartographer analysis failed"


def test_backfill_is_explicit_and_offline_injection_uses_same_pipeline(factory):
    histories = setup_scope(factory)

    class Adapter:
        async def fetch(self, query):
            return histories[query.wallet]

    with pytest.raises(CartographerError):
        asyncio.run(service(factory).backfill(MINT, observed_at=NOW))
    result = asyncio.run(service(factory).backfill(MINT, observed_at=NOW, adapter=Adapter()))
    assert result.summary.wallets_with_inbound_funding == 2


def test_backfill_failure_remains_explicit_without_losing_activity(factory):
    activity(factory)

    class Adapter:
        async def fetch(self, query):
            raise RuntimeError("untrusted provider failure")

    result = asyncio.run(service(factory).backfill(MINT, observed_at=NOW, adapter=Adapter()))
    assert "history_fetch_failed" in result.summary.warnings
    assert result.summary.wallets_with_inbound_funding is None


def test_oversized_supplied_history_scope_is_rejected(factory):
    histories = setup_scope(factory)
    with pytest.raises(CartographerError):
        service(factory, cartographer_max_wallets_per_token=1).analyze(
            MINT, histories, observed_at=NOW
        )
