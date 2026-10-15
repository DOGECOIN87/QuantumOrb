"""Offline operational endpoints, safe exports, expiry, and CLI integration."""

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from cartographer_helpers import MINT, NOW, A, B, activity, history, transfer
from sqlalchemy import select

from app.cartographer import CartographerService
from app.config import Settings
from app.db.graph import GraphRepository, make_node
from app.db.models import graph_edge_evidence, graph_edges, raw_events, scout_events
from app.db.session import create_all, create_database_engine, create_session_factory
from app.domain.enums import GraphNodeType
from app.domain.models import GraphEdgeEvidence
from app.main import create_app
from app.operations import Operations
from app.replay import ReplayService
from scripts.manage import main

PRIVATE = "synthetic-operational-test-secret"
FIXTURE = Path(__file__).parents[2] / "coding-pack/fixtures/helius/pump-create-event.json"


@pytest.fixture
def factory(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'ops.db'}")
    create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def settings():
    return Settings(_env_file=None, helius_api_key=PRIVATE)


def request(app, path):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get(path)

    return asyncio.run(run())


def populate(factory):
    activity(factory)
    observed = history(factory, transfer())
    return CartographerService(factory, settings()).analyze(MINT, {B: observed}, observed_at=NOW)


def test_readiness_details_metrics_and_summary_use_local_storage(factory):
    result = populate(factory)
    app = create_app(settings(), session_source=factory)
    assert request(app, "/health/live").json() == {"status": "live", "mode": "demo"}
    ready = request(app, "/health/ready")
    assert ready.status_code == 200
    assert ready.json()["provider_state"] == "not_read"
    assert ready.json()["counts"]["graph_edges"] == 2
    details = request(app, "/health/details")
    metrics = request(app, "/metrics")
    assert "quantumorb_database_ready 1" in metrics.text
    assert "quantumorb_graph_edges_rows 2" in metrics.text
    summary = request(app, f"/cartographer/tokens/{MINT}")
    assert summary.json()["run_id"] == result.summary.run_id
    for response in (ready, details, metrics, summary):
        assert PRIVATE not in response.text and "helius_api_key" not in response.text


def test_uninitialized_database_does_not_report_fake_zero_counts(tmp_path):
    app = create_app(Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'empty.db'}"))
    response = request(app, "/health/ready")
    assert response.status_code == 503
    assert response.json()["database_state"] == "schema_unavailable"
    metrics = request(app, "/metrics").text
    assert "quantumorb_database_ready 0" in metrics
    assert "quantumorb_raw_events_rows" not in metrics


def test_export_omits_private_metadata_raw_payloads_and_evidence_details(factory):
    GraphRepository(factory).upsert_node(
        make_node(GraphNodeType.WALLET, B, NOW, metadata={"private": PRIVATE})
    )
    populate(factory)
    exported = Operations(factory, settings()).export_graph(mint=MINT, limit=1, observed_at=NOW)
    serialized = json.dumps(exported)
    assert PRIVATE not in serialized and "payload_json" not in serialized
    assert "metadata" not in serialized and "details" not in serialized
    assert exported["redacted"] and exported["truncated"]
    assert len(exported["edges"]) == 1 and len(exported["nodes"]) <= 2
    node_ids = {node["id"] for node in exported["nodes"]}
    assert all(
        edge[key] in node_ids
        for edge in exported["edges"]
        for key in ("source_node_id", "target_node_id")
    )


def test_export_surfaces_stale_evidence_without_mutating_history(factory):
    data = history(factory, transfer())
    raw_id = data.transactions[0].raw_event_id
    observation = GraphEdgeEvidence(
        edge_type="funded_wallet",
        strength="direct",
        evidence_type="shared_funder",
        source="helius_getTransactionsForAddress",
        signature=data.transactions[0].signature,
        slot=data.transactions[0].slot,
        observed_at=NOW,
        expires_at=NOW + timedelta(seconds=1),
        algorithm_version="cartographer-0.1.0",
        details={"raw_event_ids": [raw_id], "private": PRIVATE},
    )
    GraphRepository(factory).record_relationship(
        make_node(GraphNodeType.WALLET, A, NOW),
        make_node(GraphNodeType.WALLET, B, NOW),
        observation,
    )
    exported = Operations(factory, settings()).export_graph(observed_at=NOW + timedelta(seconds=2))
    assert exported["edges"][0]["evidence_state"] == "stale"
    assert exported["edges"][0]["status"] == "expired"
    with factory() as session:
        assert session.scalar(select(graph_edges.c.status)) == "active"
        assert PRIVATE in session.scalar(select(graph_edge_evidence.c.details_json))
    assert PRIVATE not in json.dumps(exported)


@pytest.mark.parametrize(
    "path,code",
    [
        ("/graph/export?limit=0", 422),
        ("/graph/export?mint=bad", 422),
        ("/cartographer/tokens/bad", 422),
        (f"/cartographer/tokens/{MINT}", 404),
    ],
)
def test_invalid_or_unread_endpoint_scope_is_explicit(factory, path, code):
    assert request(create_app(settings(), session_source=factory), path).status_code == code


def test_fixture_replay_twice_is_idempotent_and_journal_replay_uses_same_pipeline(factory):
    replay = ReplayService(factory, settings())
    report = replay.fixtures([FIXTURE], passes=2)
    assert report.decoded == report.canonical_processed == 2 and report.failed == 0
    with factory() as session:
        assert len(session.execute(select(raw_events)).all()) == 1
        assert len(session.execute(select(scout_events)).all()) == 1
    again = replay.journal()
    assert again.decoded == 1 and again.canonical_processed == 1


def test_corrupt_raw_body_is_quarantined_without_canonical_writes(factory):
    replay = ReplayService(factory, settings())
    from app.db.raw_journal import RawJournal
    from app.integrations.fixture_transport import FixtureTransport

    raw = RawJournal(factory).insert(FixtureTransport([FIXTURE]).load_all()[0].envelope)
    with factory() as session:
        session.execute(
            raw_events.update().where(raw_events.c.id == raw.id).values(payload_json="{}")
        )
        session.commit()
    report = replay.journal()
    assert report.failed == 1 and report.warnings == ["replay_payload_hash_mismatch"]
    with factory() as session:
        assert not session.execute(select(scout_events)).all()


def test_real_cli_migration_replay_analysis_export_and_no_overwrite(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    database = f"sqlite:///{tmp_path / 'cli.db'}"
    prefix = ["--database-url", database]
    assert main(prefix + ["init-db"]) == 0
    assert main(prefix + ["replay", str(FIXTURE), "--passes", "2"]) == 0
    mint = json.loads(FIXTURE.read_text())["raw_payload"]["decoded"]["mint"]
    assert main(prefix + ["analyze-token", mint]) == 0
    destination = tmp_path / "graph.json"
    assert main(prefix + ["export-graph", "--output", str(destination)]) == 0
    before = destination.read_text()
    assert json.loads(before)["redacted"]
    assert main(prefix + ["export-graph", "--output", str(destination)]) == 2
    assert destination.read_text() == before
    assert main(prefix + ["health"]) == 0
    assert PRIVATE not in capsys.readouterr().out


def test_cli_live_commands_reject_demo_configuration(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("INGEST_MODE", "demo")
    monkeypatch.setenv("INGEST_TRANSPORT", "fixture")
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    assert main(["listen", "--live"]) == 2
    assert main(["analyze-token", MINT, "--live"]) == 2
    assert "Traceback" not in capsys.readouterr().err


def test_fixture_replay_respects_explicit_bounds_before_writes(factory):
    from app.replay import ReplayError

    with pytest.raises(ReplayError):
        ReplayService(factory, settings()).fixtures([FIXTURE, FIXTURE], limit=1)
    with factory() as session:
        assert not session.execute(select(raw_events)).all()
