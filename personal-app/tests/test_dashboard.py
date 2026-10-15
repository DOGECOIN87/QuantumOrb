"""Read-only dashboard projections use actual stored data and preserve unknown states."""

import asyncio
import json
import socket
from datetime import timedelta

import httpx
import pytest
from cartographer_helpers import NOW, A, B, activity
from sqlalchemy import func, select

from app.cartographer import CartographerService
from app.config import Settings
from app.dashboard import DashboardRepository
from app.db.graph import GraphRepository, make_node
from app.db.models import raw_events, scout_events, token_lifecycles, tokens
from app.db.session import create_all, create_database_engine, create_session_factory
from app.domain.enums import GraphNodeType, Network
from app.main import create_app
from app.replay import ReplayService

PRIVATE = "synthetic-dashboard-secret"


@pytest.fixture
def factory(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'dashboard.db'}")
    create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def settings():
    return Settings(_env_file=None, helius_api_key=PRIVATE)


def record_activity(factory, **kwargs):
    event = activity(factory, **kwargs)
    with factory() as session:
        session.execute(
            scout_events.update()
            .where(scout_events.c.event_key == event.event_key)
            .values(observed_at=event.block_time)
        )
        session.commit()
    return event


def request(app, path):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get(path)

    return asyncio.run(run())


def seed(factory):
    from pathlib import Path

    fixture = Path(__file__).parents[2] / "coding-pack/fixtures/helius/pump-create-event.json"
    ReplayService(factory, settings()).fixtures([fixture], passes=2)
    return json.loads(fixture.read_text())["raw_payload"]["decoded"]["mint"]


def test_snapshot_counts_known_empty_storage_but_keeps_missing_evidence_unknown(factory):
    result = DashboardRepository(factory, settings()).snapshot(now=NOW)
    assert result.counters["events"] == result.counters["launches_24h"] == 0
    assert result.last_observation_at is result.latest_slot is None
    assert all(item["events"] == 0 for item in result.series)
    assert len(result.series) == 24
    assert result.agents[0]["state"] == "not_read"
    assert all(agent["metric"] is None for agent in result.agents[2:])
    assert result.health["provider_state"] == "not_read"


def test_missing_schema_has_unknown_counts_instead_of_fake_zeros(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'missing.db'}")
    try:
        result = DashboardRepository(create_session_factory(engine), settings()).snapshot(now=NOW)
        assert not result.health["ready"]
        assert all(value is None for value in result.counters.values())
        assert result.series == []
        assert "database_unavailable" in result.warnings
    finally:
        engine.dispose()


def test_hourly_counts_slot_finality_freshness_and_search_use_real_rows(factory):
    record_activity(factory, wallet=B, creator=A, event_type="token_created", finality="finalized")
    record_activity(factory, wallet=B, age=4000, number=91)
    record_activity(factory, wallet=A, age=90000, number=92, finality="reorged_or_changed")
    repo = DashboardRepository(factory, settings())
    snapshot = repo.snapshot(now=NOW)
    assert snapshot.counters["events"] == 3
    assert snapshot.counters["launches_24h"] == 1
    assert snapshot.counters["wallets_1h"] == 1
    assert snapshot.counters["finalized"] == 2
    assert sum(item["events"] for item in snapshot.series) == 2
    assert snapshot.latest_slot is not None
    assert repo.activity(q=B, window="1h", now=NOW).total == 1
    assert repo.activity(finality="reorged_or_changed").total == 1
    wallets = repo.wallets(now=NOW)
    assert wallets.total == 1 and wallets.items[0].event_count == 2
    assert wallets.items[0].token_count == 1
    assert snapshot.agents[0]["state"] == "observed"


def test_stale_data_is_not_labeled_fresh(factory):
    record_activity(factory, age=600)
    result = DashboardRepository(factory, settings()).snapshot(now=NOW)
    assert "observations_stale" in result.warnings
    assert "provider_connection_not_verified" in result.warnings


def test_activity_pagination_is_stable_and_does_not_drop_same_time_events(factory):
    for index in range(3):
        record_activity(factory, number=91 + index)
    repo = DashboardRepository(factory, settings())
    first = repo.activity(limit=2)
    second = repo.activity(limit=2, offset=2)
    assert first.total == second.total == 3
    assert first.has_more and not second.has_more
    assert {event.event_key for event in first.items}.isdisjoint(
        event.event_key for event in second.items
    )


def test_tokens_lifecycle_public_address_search_and_missing_analysis(factory):
    mint = seed(factory)
    repo = DashboardRepository(factory, settings())
    result = repo.token_list(q=mint[:10], lifecycle="seen_create")
    assert result.total == 1 and result.items[0].mint == mint
    assert result.items[0].provisional and result.items[0].event_count == 1
    assert repo.token_list(lifecycle="pool_active").total == 0
    assert repo.wallets(q=result.items[0].creator[:8]).total in (0, 1)
    assert repo.snapshot().counters["analyses"] == 0
    CartographerService(factory, settings()).analyze(mint, observed_at=NOW)
    assert repo.snapshot().counters["analyses"] == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 0},
        {"limit": 101},
        {"offset": -1},
        {"offset": 100001},
        {"q": "x" * 89},
        {"q": "' OR 1=1 --"},
        {"q": "<script>"},
    ],
)
def test_repository_bounds_and_query_syntax_fail_before_storage(factory, kwargs):
    repo = DashboardRepository(factory, settings())
    for method in (repo.activity, repo.token_list, repo.wallets):
        with pytest.raises(ValueError):
            method(**kwargs)


@pytest.mark.parametrize(
    "path",
    [
        "/api/activity?limit=101",
        "/api/activity?offset=-1",
        "/api/activity?window=wrong",
        "/api/activity?finality=trusted",
        "/api/activity?q=%3Cscript%3E",
        "/api/tokens?lifecycle=guaranteed",
        "/api/tokens?q=%25",
        "/api/wallets?limit=0",
    ],
)
def test_http_filters_reject_invalid_inputs(factory, path):
    assert request(create_app(settings(), session_source=factory), path).status_code == 422


def test_browser_assets_and_data_routes_are_safe_offline_and_read_only(factory, monkeypatch):
    mint = seed(factory)
    app = create_app(settings(), session_source=factory)

    def no_network(*args, **kwargs):
        pytest.fail("dashboard read attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    with factory() as session:
        session.execute(tokens.update().values(name=PRIVATE, metadata_uri=f"https://{PRIVATE}"))
        session.commit()
        before = [
            session.scalar(select(func.count()).select_from(table))
            for table in (raw_events, scout_events, tokens, token_lifecycles)
        ]
    for path in (
        "/",
        "/assets/dashboard.css",
        "/assets/dashboard.js",
        "/assets/orb.mjs",
        "/assets/demo.mjs",
        "/api/dashboard",
        "/api/tokens",
        "/api/wallets",
        f"/api/activity?q={mint}",
    ):
        response = request(app, path)
        assert response.status_code == 200, path
        assert PRIVATE not in response.text
        if path.startswith("/api/"):
            assert "payload_json" not in response.text and "metadata_uri" not in response.text
    root = request(app, "/")
    assert "script-src 'self'" in root.headers["content-security-policy"]
    assert '<script type="module" src="/assets/dashboard.js">' in root.text
    assert "onclick=" not in root.text
    assert 'class="intro"' not in root.text
    with factory() as session:
        after = [
            session.scalar(select(func.count()).select_from(table))
            for table in (raw_events, scout_events, tokens, token_lifecycles)
        ]
    assert before == after


def test_window_boundary_is_inclusive(factory):
    record_activity(factory, age=3600)
    repo = DashboardRepository(factory, settings())
    assert repo.activity(window="1h", now=NOW).total == 1
    assert repo.activity(window="1h", now=NOW + timedelta(microseconds=1)).total == 0


@pytest.mark.parametrize("path", ["/api/dashboard", "/api/activity", "/api/tokens", "/api/wallets"])
def test_failed_storage_has_safe_errors_without_exception_values(path):
    def unavailable_source():
        raise RuntimeError(PRIVATE)

    response = request(create_app(settings(), session_source=unavailable_source), path)
    assert response.status_code == 503
    assert PRIVATE not in response.text


def test_static_asset_traversal_does_not_expose_application_source(factory):
    response = request(create_app(settings(), session_source=factory), "/assets/%2e%2e/config.py")
    assert response.status_code == 404


def test_graph_counts_respect_the_configured_network(factory):
    graph = GraphRepository(factory)
    graph.upsert_node(make_node(GraphNodeType.WALLET, A, NOW))
    graph.upsert_node(make_node(GraphNodeType.WALLET, B, NOW, network=Network.DEVNET))
    result = DashboardRepository(factory, settings()).snapshot(now=NOW)
    assert result.counters["nodes"] == 1
    assert result.counters["relationships"] == 0
