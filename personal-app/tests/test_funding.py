"""Funding direction, ordering, coverage, and raw-to-graph golden integration."""

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from cartographer_helpers import MINT, NOW, TOKEN_2022, A, B, C, activity, history, transfer
from sqlalchemy import select

from app.config import Settings
from app.db.graph import GraphRepository
from app.db.models import graph_edge_evidence
from app.db.session import create_all, create_database_engine, create_session_factory
from app.funding import FundingDetector, FundingError


@pytest.fixture
def factory(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'funding.db'}")
    create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def analyze(factory, result, wallet=B, **settings):
    return FundingDetector(factory, Settings(_env_file=None, **settings)).analyze(
        MINT, wallet, result, observed_at=NOW
    )


def test_single_funder_matches_golden_and_replay_is_idempotent(factory):
    activity(factory, slot=300000020)
    data = history(factory, transfer())
    first = analyze(factory, data)
    second = analyze(factory, data)
    assert first.history_complete and first.warnings == ()
    assert len(first.links) == 1 and second.edges_added == second.nodes_added == 0
    link = first.links[0]
    assert (link.source_wallet, link.target_wallet, link.fact.amount_raw) == (A, B, "700")
    assert link.time_delta_seconds == 60
    with factory() as session:
        rows = session.execute(select(graph_edge_evidence)).mappings().all()
    assert len(rows) == 1
    observation = json.loads(rows[0]["details_json"])["observation"]
    golden = json.loads(
        (
            Path(__file__).parents[2]
            / "coding-pack/golden-tests/cartographer-funding.expected.json"
        ).read_text()
    )
    assert {key: observation[key] for key in golden} == golden
    assert observation["details"]["label"] == "funding_link_observed"


def test_one_funder_can_fund_two_wallets_without_ownership_claims(factory):
    for number, wallet in enumerate((B, C), 90):
        activity(factory, wallet=wallet, number=number, slot=300000020)
        result = analyze(
            factory,
            history(factory, transfer(recipient=wallet, number=number - 89), wallet=wallet),
            wallet,
        )
        assert len(result.links) == 1
    edges = GraphRepository(factory).edges()
    assert len(edges) == 2 and all(edge.edge_type.value == "funded_wallet" for edge in edges)


@pytest.mark.parametrize("slot,index,expected", [(30, 0, 0), (20, 0, 1), (20, 2, 0), (10, 0, 1)])
def test_funding_must_precede_activity(factory, slot, index, expected):
    activity(factory, slot=300000020)
    result = analyze(factory, history(factory, transfer(slot=slot, index=index)))
    assert len(result.links) == expected


def test_same_slot_without_activity_index_stays_unknown(factory):
    activity(factory, slot=300000020, index=None)
    result = analyze(factory, history(factory, transfer(slot=20)))
    assert not result.links and "funding_order_unavailable" in result.warnings


@pytest.mark.parametrize(
    "program,fee,count",
    [(None, None, 1), (TOKEN_2022, None, 0), (TOKEN_2022, 7, 1), (TOKEN_2022, 700, 0)],
)
def test_token_owners_and_net_credit_are_required(factory, program, fee, count):
    activity(factory, slot=300000020)
    options = {"program": program} if program else {}
    result = analyze(factory, history(factory, transfer(token=True, fee=fee, **options)))
    assert len(result.links) == count
    if fee == 7:
        assert result.links[0].fact.net_amount_raw == "693"
    if program == TOKEN_2022 and fee is None:
        assert "funding_net_amount_unavailable" in result.warnings


def test_partial_history_keeps_known_facts_with_coverage_warning(factory):
    activity(factory, slot=300000020)
    result = analyze(factory, history(factory, transfer(), token="more"))
    assert len(result.links) == 1 and not result.history_complete
    assert "history_limit_reached" in result.warnings


def test_history_cannot_forge_durable_coverage(factory):
    activity(factory, slot=300000020)
    data = history(factory, transfer(), token="more")
    fake = replace(data, coverage=replace(data.coverage, history_complete=True))
    with pytest.raises(FundingError):
        analyze(factory, fake)
    assert not GraphRepository(factory).edges()


def test_history_cannot_hide_returned_transactions(factory):
    activity(factory)
    data = history(factory, transfer(), transfer(slot=11, number=2))
    with pytest.raises(FundingError):
        analyze(factory, replace(data, transactions=data.transactions[:1]))


def test_same_transaction_funding_requires_earlier_instruction(factory):
    activity(factory, slot=300000010, index=0, instruction_index=1, number=1)
    assert len(analyze(factory, history(factory, transfer())).links) == 1


def test_delegate_is_not_substituted_for_missing_owner(factory):
    activity(factory)
    data = transfer(token=True)
    for name in ("preTokenBalances", "postTokenBalances"):
        data["meta"][name][0].pop("owner")
    result = analyze(factory, history(factory, data))
    assert not result.links and "funding_owner_unavailable" in result.warnings


def test_provisional_activity_is_visible_and_changed_activity_is_unavailable(factory):
    activity(factory, finality="provisional")
    result = analyze(factory, history(factory, transfer()))
    assert "provisional_token_activity" in result.warnings
    assert not result.history_complete

    activity(factory, wallet=C, number=91, finality="reorged_or_changed")
    with pytest.raises(FundingError):
        analyze(factory, history(factory, transfer(recipient=C, number=2), wallet=C), C)


def test_exact_configured_lookback_boundary_is_enforced(factory):
    activity(factory)
    data = history(factory, transfer())
    altered = replace(
        data,
        query=data.query.model_copy(
            update={"start_time": data.query.end_time - timedelta(days=1, seconds=1)}
        ),
    )
    with pytest.raises(FundingError) as error:
        analyze(factory, altered, cartographer_lookback_days=1)
    assert error.value.code == "funding_history_limit"


def test_no_activity_or_wrong_wallet_cannot_create_edges(factory):
    data = history(factory, transfer())
    with pytest.raises(FundingError):
        analyze(factory, data)
    activity(factory, slot=300000020)
    with pytest.raises(FundingError):
        analyze(factory, data, C)


def test_small_transfer_threshold_retains_only_available_evidence(factory):
    activity(factory, slot=300000020)
    result = analyze(factory, history(factory, transfer()), cartographer_min_funding_lamports=1000)
    assert len(result.links) == 1
    assert "below_minimum_funding_only_evidence" in result.warnings
    data = history(factory, transfer(), transfer(amount=2000, slot=11, number=2))
    result = analyze(factory, data, cartographer_min_funding_lamports=1000)
    assert [link.fact.amount_raw for link in result.links] == ["2000"]


def test_counterparty_limit_is_visible(factory):
    activity(factory, slot=300000020)
    data = history(factory, transfer(), transfer(sender=C, slot=11, number=2))
    result = analyze(factory, data, cartographer_max_counterparties_per_wallet=1)
    assert len(result.links) == 1 and "counterparty_limit_reached" in result.warnings


def test_negative_time_delta_is_not_funding(factory):
    activity(factory, slot=300000020)
    result = analyze(factory, history(factory, transfer(age=30)))
    assert not result.links and "funding_time_conflict" in result.warnings


def test_naive_observation_time_is_rejected(factory):
    activity(factory, slot=300000020)
    detector = FundingDetector(factory, Settings(_env_file=None))
    with pytest.raises(FundingError):
        detector.analyze(
            MINT, B, history(factory, transfer()), observed_at=NOW.replace(tzinfo=None)
        )
