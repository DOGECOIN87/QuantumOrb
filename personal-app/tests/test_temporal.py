"""Transaction/slot evidence, scope bounds, uncertainty, and immutable provenance."""

import json
from pathlib import Path

import pytest
from cartographer_helpers import MINT, NOW, A, B, C, activity, address, history, transfer
from sqlalchemy import select

from app.config import Settings
from app.db.graph import GraphRepository
from app.db.models import graph_edge_evidence
from app.db.session import create_all, create_database_engine, create_session_factory
from app.temporal import TemporalDetector


@pytest.fixture
def factory(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'temporal.db'}")
    create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def analyze(factory, *, histories=(), **settings):
    return TemporalDetector(factory, Settings(_env_file=None, **settings)).analyze(
        MINT, observed_at=NOW, histories=histories
    )


def test_explicit_roles_in_one_transaction_are_direct_and_replay_is_idempotent(factory):
    activity(factory, wallet=B, creator=C)
    first = analyze(factory)
    replay = analyze(factory)
    assert first.shared_transaction_groups == 1 and first.same_slot_groups == 0
    assert replay.edges_added == replay.nodes_added == 0
    assert first.same_bundle_groups is None and first.warnings == ("bundle_data_unavailable",)
    (edge,) = GraphRepository(factory).edges()
    assert edge.edge_type.value == "shared_transaction" and edge.strength.value == "direct"
    with factory() as session:
        rows = session.execute(select(graph_edge_evidence)).mappings().all()
    assert len(rows) == 1
    details = json.loads(rows[0]["details_json"])["observation"]["details"]
    assert {role["role"] for roles in details["roles"].values() for role in roles} == {
        "user",
        "creator",
    }


def test_same_slot_across_distinct_transactions_stays_weak_and_never_becomes_bundle(factory):
    activity(factory, wallet=B)
    activity(factory, wallet=C, number=91)
    result = analyze(factory)
    assert result.shared_transaction_groups == 0 and result.same_slot_groups == 1
    (edge,) = GraphRepository(factory).edges()
    golden = json.loads(
        (
            Path(__file__).parents[2] / "coding-pack/golden-tests/uncertainty.expected.json"
        ).read_text()
    )
    assert edge.strength.value == golden["same_slot_only_strength"]
    assert edge.edge_type.value == "same_slot_activity"
    assert result.same_bundle_groups is None
    assert golden["bundle_without_bundle_id"] == "not_created"


def test_different_token_scopes_cannot_create_slot_links(factory):
    activity(factory, wallet=B)
    activity(factory, wallet=C, mint=address(99), number=91)
    result = analyze(factory)
    assert not result.same_slot_groups and not GraphRepository(factory).edges()


def test_transfer_only_participant_is_moderate(factory):
    activity(factory, wallet=B, slot=300000010, number=1)
    result = analyze(factory, histories=[history(factory, transfer())])
    assert result.shared_transaction_groups == 1
    (edge,) = GraphRepository(factory).edges()
    assert edge.strength.value == "moderate"


def test_missing_token_owner_does_not_become_a_delegate_role(factory):
    activity(factory, wallet=B, slot=300000010, number=1)
    data = transfer(token=True)
    for name in ("preTokenBalances", "postTokenBalances"):
        data["meta"][name][0].pop("owner")
    result = analyze(factory, histories=[history(factory, data)])
    assert not GraphRepository(factory).edges()
    assert "temporal_owner_unavailable" in result.warnings


def test_provisional_roles_need_review(factory):
    activity(factory, wallet=B, creator=C, finality="provisional")
    result = analyze(factory)
    (edge,) = GraphRepository(factory).edges()
    assert edge.status.value == "needs_review"
    assert "provisional_token_activity" in result.warnings and not result.complete


def test_reorged_roles_do_not_create_edges(factory):
    activity(factory, wallet=B, creator=C, finality="reorged_or_changed")
    result = analyze(factory)
    assert not GraphRepository(factory).edges()
    assert "token_activity_not_read" in result.warnings


def test_pair_and_wallet_limits_are_explicit(factory):
    activity(factory, wallet=A, creator=B)
    activity(factory, wallet=C, number=90, event_index=1)
    result = analyze(factory, cartographer_max_temporal_edges=1)
    assert len(GraphRepository(factory).edges()) == 1
    assert "temporal_edge_limit_reached" in result.warnings
    result = analyze(factory, cartographer_max_wallets_per_token=2)
    assert "wallet_limit_reached" in result.warnings


def test_exact_pair_budget_does_not_invent_missing_coverage(factory):
    activity(factory, wallet=A, creator=B)
    result = analyze(factory, cartographer_max_temporal_edges=1)
    assert result.complete and "temporal_edge_limit_reached" not in result.warnings


def test_event_budget_and_counterparty_budget_are_explicit(factory):
    activity(factory, wallet=A, creator=B)
    activity(factory, wallet=C, number=90, event_index=1)
    result = analyze(factory, cartographer_max_history_tx=1)
    assert "temporal_event_limit_reached" in result.warnings
    result = analyze(factory, cartographer_max_counterparties_per_wallet=1)
    assert "counterparty_limit_reached" in result.warnings


def test_empty_scope_preserves_not_read(factory):
    result = analyze(factory)
    assert not result.complete and "token_activity_not_read" in result.warnings
    assert result.same_bundle_groups is None


def test_partial_history_keeps_known_roles_and_coverage_warnings(factory):
    activity(factory, wallet=B, slot=300000010, number=1)
    result = analyze(factory, histories=[history(factory, transfer(), token="more")])
    assert result.shared_transaction_groups == 1 and not result.complete
    assert "history_limit_reached" in result.warnings
    assert "temporal_history_incomplete" in result.warnings


def test_conflicting_history_slot_quarantines_the_transaction(factory):
    activity(factory, wallet=B, creator=C, slot=300000010, number=1)
    result = analyze(factory, histories=[history(factory, transfer(slot=11))])
    assert not GraphRepository(factory).edges()
    assert "transaction_context_conflict" in result.warnings
