"""Graph identity, provenance, append-only evidence, and transaction boundaries."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.graph import ALGORITHM_VERSION, GraphError, GraphRepository, make_node
from app.db.models import graph_edge_evidence, graph_edges, graph_nodes
from app.db.rpc_journal import RpcResponseJournal
from app.db.session import create_all, create_database_engine, create_session_factory
from app.domain.enums import (
    GraphEdgeStatus,
    GraphEdgeStrength,
    GraphNodeType,
    Network,
)
from app.domain.models import GraphEdgeEvidence

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
A, B = "1" * 32, "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SIGNATURE = "1" * 64


@pytest.fixture
def factory(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'graph.db'}")
    create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def nodes():
    return [make_node(GraphNodeType.WALLET, address, NOW) for address in (A, B)]


def evidence(factory, **changes):
    raw = RpcResponseJournal(factory, "helius_rpc", "test").insert(
        method="getTransaction",
        program_id=A,
        params=[SIGNATURE],
        response={"synthetic": True},
        received_at=NOW,
        signature=SIGNATURE,
        slot=7,
    )
    return GraphEdgeEvidence(
        **{
            "edge_type": "funded_wallet",
            "strength": "direct",
            "evidence_type": "shared_funder",
            "source": "helius_getTransactionsForAddress",
            "signature": SIGNATURE,
            "slot": 7,
            "details": {"raw_event_ids": [raw.id], "amount_raw": "18446744073709551615"},
            "observed_at": NOW,
            "algorithm_version": ALGORITHM_VERSION,
            **changes,
        }
    )


def rows(factory, table):
    with factory() as session:
        return list(session.execute(select(table)).mappings())


def test_repeated_facts_do_not_duplicate_nodes_edges_or_evidence(factory):
    repository = GraphRepository(factory)
    observation = evidence(factory)
    first = repository.record_relationship(*nodes(), observation)
    duplicate = repository.record_relationship(*nodes(), observation)
    assert (first.nodes_added, first.edges_added, first.evidence_added) == (2, 1, True)
    assert (duplicate.nodes_added, duplicate.edges_added, duplicate.evidence_added) == (0, 0, False)
    assert first.evidence_id == duplicate.evidence_id
    assert first.edge == duplicate.edge
    assert len(repository.nodes()) == 2 and len(repository.edges()) == 1
    stored = rows(factory, graph_edge_evidence)
    assert len(stored) == 1
    assert json.loads(stored[0]["details_json"])["observation"]["details"]["amount_raw"] == (
        "18446744073709551615"
    )
    assert stored[0]["signature"] == SIGNATURE


def test_undirected_pair_is_canonical_but_directed_reverse_is_distinct(factory):
    repository = GraphRepository(factory)
    observation = evidence(factory)
    first = repository.record_relationship(*nodes(), observation, direction="undirected")
    reverse = repository.record_relationship(
        *reversed(nodes()), observation, direction="undirected"
    )
    assert first.edge.id == reverse.edge.id and not reverse.evidence_added
    directed = repository.record_relationship(*nodes(), observation)
    other = repository.record_relationship(*reversed(nodes()), observation)
    assert len({first.edge.id, directed.edge.id, other.edge.id}) == 3


def test_evidence_changes_append_without_overwriting_and_old_replay_cannot_reactivate(factory):
    repository = GraphRepository(factory)
    original = evidence(factory)
    first = repository.record_relationship(*nodes(), original)
    before = dict(rows(factory, graph_edge_evidence)[0])
    later = evidence(factory, observed_at=NOW + timedelta(hours=1))
    retracted = repository.record_relationship(*nodes(), later, status="retracted")
    assert retracted.edge.id == first.edge.id and retracted.evidence_added
    replay = repository.record_relationship(*nodes(), original)
    assert replay.edge.status is GraphEdgeStatus.RETRACTED
    assert dict(rows(factory, graph_edge_evidence)[0]) == before
    assert len(rows(factory, graph_edge_evidence)) == 2


def test_receipt_time_only_change_does_not_refresh_evidence(factory):
    repository = GraphRepository(factory)
    original = evidence(factory)
    first = repository.record_relationship(*nodes(), original)
    replay = repository.record_relationship(
        *nodes(), evidence(factory, observed_at=NOW + timedelta(hours=1))
    )
    assert not replay.evidence_added
    assert replay.edge.last_observed_at == first.edge.last_observed_at == NOW


def test_new_older_evidence_extends_history_without_downgrading_current_status(factory):
    repository = GraphRepository(factory)
    observation = evidence(factory)
    repository.record_relationship(*nodes(), observation, status="retracted")
    earlier = evidence(factory, observed_at=NOW - timedelta(hours=1), evidence_type="other")
    result = repository.record_relationship(*nodes(), earlier)
    assert result.edge.first_observed_at == NOW - timedelta(hours=1)
    assert result.edge.last_observed_at == NOW
    assert result.edge.status is GraphEdgeStatus.RETRACTED


@pytest.mark.parametrize(
    "change",
    [
        {"signature": "2" * 64},
        {"slot": 8},
        {"details": {"raw_event_ids": [999999]}},
        {"details": {"raw_event_ids": [True]}},
        {"details": {}},
        {"event_key": "a" * 64},
    ],
)
def test_missing_or_conflicting_provenance_is_atomic(factory, change):
    observation = evidence(factory)
    observation = observation.model_copy(update=change)
    with pytest.raises((GraphError, ValueError)):
        GraphRepository(factory).record_relationship(*nodes(), observation)
    assert not rows(factory, graph_nodes)
    assert not rows(factory, graph_edges)
    assert not rows(factory, graph_edge_evidence)


@pytest.mark.parametrize("strength", ["direct", "strong", "moderate", "unknown"])
def test_same_slot_evidence_cannot_be_promoted(factory, strength):
    with pytest.raises(GraphError, match="graph observation") as error:
        GraphRepository(factory).record_relationship(
            *nodes(), evidence(factory, edge_type="same_slot_activity", strength=strength)
        )
    assert error.value.code == "same_slot_requires_weak_strength"


def test_same_slot_stays_weak_and_bundle_requires_a_documented_source(factory):
    repo = GraphRepository(factory)
    result = repo.record_relationship(
        *nodes(), evidence(factory, edge_type="same_slot_activity", strength="weak")
    )
    assert result.edge.strength is GraphEdgeStrength.WEAK
    with pytest.raises(GraphError) as error:
        repo.record_relationship(*nodes(), evidence(factory, edge_type="same_bundle_activity"))
    assert error.value.code == "bundle_data_unavailable"
    with pytest.raises(GraphError):
        make_node(GraphNodeType.BUNDLE, "invented-bundle", NOW)


def test_global_node_key_cannot_mix_networks_or_partial_commit(factory):
    repo = GraphRepository(factory)
    repo.upsert_node(nodes()[1])
    source = make_node(GraphNodeType.WALLET, A, NOW, network=Network.DEVNET)
    target = make_node(GraphNodeType.WALLET, B, NOW, network=Network.DEVNET)
    with pytest.raises(GraphError):
        repo.record_relationship(source, target, evidence(factory))
    assert len(repo.nodes()) == 1
    assert repo.nodes(network=Network.DEVNET) == ()


def test_caller_rollback_owns_nodes_and_evidence(factory):
    observation = evidence(factory)
    with factory() as session:
        repo = GraphRepository(session)
        repo.record_relationship(*nodes(), observation)
        assert len(repo.edges()) == 1
        session.rollback()
    assert not GraphRepository(factory).nodes()
    assert not rows(factory, graph_edge_evidence)


def test_concurrent_replays_share_one_evidence_row(factory):
    observation = evidence(factory)
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(
            workers.map(
                lambda _: GraphRepository(factory).record_relationship(*nodes(), observation),
                range(8),
            )
        )
    assert len({result.edge.id for result in results}) == 1
    assert len(rows(factory, graph_edge_evidence)) == 1


@pytest.mark.parametrize(
    "kind,identifier",
    [
        (GraphNodeType.WALLET, "invalid"),
        (GraphNodeType.TRANSACTION, A),
        (GraphNodeType.METADATA_URI, "https://private.example/item"),
    ],
)
def test_node_identifiers_validate_by_kind(kind, identifier):
    with pytest.raises((GraphError, ValueError)):
        make_node(kind, identifier, NOW)


def test_node_times_metadata_and_identity_are_validated(factory):
    repo = GraphRepository(factory)
    first = nodes()[0].model_copy(update={"metadata": {"first": "preserved"}})
    repo.upsert_node(first)
    later = make_node(GraphNodeType.WALLET, A, NOW + timedelta(hours=1))
    stored = repo.upsert_node(later)
    assert stored.metadata == {"first": "preserved"}
    assert stored.last_seen_at == NOW + timedelta(hours=1)
    for update in (
        {"node_key": "wrong"},
        {"last_seen_at": NOW - timedelta(seconds=1)},
        {"metadata": {"bad": float("nan")}},
    ):
        with pytest.raises(GraphError):
            repo.upsert_node(first.model_copy(update=update))


def test_optional_signature_remains_nullable():
    observation = GraphEdgeEvidence(
        edge_type="same_metadata_uri",
        strength="weak",
        evidence_type="metadata",
        source="scout",
        signature=None,
        observed_at=NOW,
        algorithm_version=ALGORITHM_VERSION,
    )
    assert observation.signature is None
