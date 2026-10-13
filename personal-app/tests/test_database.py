from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command
from app.db.base import Base
from app.db.session import create_database_engine

EXPECTED_TABLES = {
    "raw_events",
    "tokens",
    "launches",
    "scout_events",
    "token_lifecycles",
    "evidence",
    "score_snapshots",
    "alerts",
    "outcomes",
    "graph_nodes",
    "graph_edges",
    "graph_edge_evidence",
    "provider_catalog_snapshots",
    "reconciliation_records",
    "cursors",
    "job_runs",
}


def test_fresh_metadata_contains_all_required_tables_and_indexes() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == EXPECTED_TABLES
    raw_indexes = {index["name"] for index in inspector.get_indexes("raw_events")}
    assert "ix_raw_events_signature_slot" in raw_indexes
    assert "ix_raw_events_status_received_at" in raw_indexes
    edge_indexes = {index["name"] for index in inspector.get_indexes("graph_edge_evidence")}
    assert {
        "ix_graph_edge_evidence_edge_observed",
        "ix_graph_edge_evidence_signature",
        "ix_graph_edge_evidence_event_key",
    } <= edge_indexes


def test_sqlite_engine_enables_foreign_keys_and_wal(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'quantumorb.db'}"
    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert connection.execute(text("PRAGMA journal_mode")).scalar() == "wal"


def test_alembic_upgrade_and_downgrade_create_and_remove_schema(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'migration.db'}"
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    assert set(inspect(engine).get_table_names()) == EXPECTED_TABLES | {"alembic_version"}
    command.downgrade(config, "base")
    assert inspect(engine).get_table_names() == ["alembic_version"]
