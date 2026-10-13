"""SQLAlchemy table metadata matching coding-pack/database/SCHEMA.md."""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
)

from app.db.base import Base

metadata = Base.metadata


def _dt(name: str, *, nullable: bool = False) -> Column[Any]:
    return Column(name, DateTime(timezone=True), nullable=nullable)


def _json(name: str, *, nullable: bool = False) -> Column[Any]:
    return Column(name, Text, nullable=nullable)


raw_events = Table(
    "raw_events",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("event_key", String(64), nullable=False, unique=True),
    Column("provider", String(32), nullable=False),
    Column("transport", String(32), nullable=False),
    Column("notification_id", Text, nullable=False),
    Column("signature", String(88)),
    Column("slot", Integer),
    Column("commitment", String(16)),
    _dt("received_at"),
    _json("payload_json"),
    Column("payload_sha256", String(64), nullable=False),
    Column("status", String(16), nullable=False),
    Column("safe_error_code", String(128)),
    Column("safe_error_message", Text),
    Column("idl_revision", Text),
    _dt("created_at"),
    CheckConstraint("slot IS NULL OR slot >= 0", name="raw_events_slot_nonnegative"),
    CheckConstraint("length(payload_sha256) = 64", name="raw_events_payload_sha256_length"),
    Index("ix_raw_events_signature_slot", "signature", "slot"),
    Index("ix_raw_events_status_received_at", "status", "received_at"),
)

tokens = Table(
    "tokens",
    metadata,
    Column("mint", String(64), primary_key=True),
    Column("symbol", String(64)),
    Column("name", Text),
    Column("metadata_uri", Text),
    Column("decimals", Integer),
    Column("supply_raw", Text),
    Column("supply_ui", Numeric),
    Column("mint_authority", String(64)),
    Column("freeze_authority", String(64)),
    Column("creator_wallet", String(64)),
    _dt("first_seen_at"),
    _dt("last_seen_at"),
    Column("metadata_status", String(16), nullable=False),
    Column("last_indexed_slot", Integer),
    Column("last_metadata_source", Text),
    _dt("last_metadata_observed_at", nullable=True),
    Column("raw_asset_json", Text),
    _dt("created_at"),
    _dt("updated_at"),
)

launches = Table(
    "launches",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("mint", String(64), ForeignKey("tokens.mint"), nullable=False),
    Column("launch_signature", String(88), nullable=False, unique=True),
    Column("launch_event_key", String(64), nullable=False, unique=True),
    Column("launch_slot", Integer, nullable=False),
    _dt("launch_time", nullable=True),
    Column("source_program", String(32), nullable=False),
    Column("creator_wallet", String(64)),
    Column("user_wallet", String(64)),
    Column("bonding_curve", String(64)),
    Column("quote_mint", String(64)),
    Column("detection_status", String(16), nullable=False),
    _dt("first_detected_at"),
    _dt("last_updated_at"),
    Column("raw_event_id", Integer, ForeignKey("raw_events.id"), nullable=False),
    CheckConstraint("launch_slot >= 0", name="launches_slot_nonnegative"),
    Index("ix_launches_mint_slot", "mint", "launch_slot"),
    Index("ix_launches_creator_slot", "creator_wallet", "launch_slot"),
    Index("ix_launches_detection_updated", "detection_status", "last_updated_at"),
)

scout_events = Table(
    "scout_events",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("event_key", String(64), nullable=False, unique=True),
    Column("raw_event_id", Integer, ForeignKey("raw_events.id"), nullable=False),
    Column("signature", String(88), nullable=False),
    Column("slot", Integer, nullable=False),
    _dt("block_time", nullable=True),
    _dt("observed_at"),
    Column("commitment", String(16), nullable=False),
    Column("program", String(16), nullable=False),
    Column("program_id", String(64), nullable=False),
    Column("event_name", Text, nullable=False),
    Column("event_type", String(32), nullable=False),
    Column("mint", String(64)),
    Column("user", String(64)),
    Column("creator", String(64)),
    Column("bonding_curve", String(64)),
    Column("pool", String(64)),
    Column("quote_mint", String(64)),
    Column("transaction_index", Integer),
    Column("instruction_index", Integer),
    Column("event_index", Integer, nullable=False, default=0),
    _json("payload_json"),
    Column("decode_status", String(16), nullable=False),
    Column("finality_status", String(24), nullable=False),
    Column("reconciliation_status", String(16), nullable=False),
    Column("derivation_version", String(64), nullable=False),
    _dt("created_at"),
    Index("ix_scout_events_mint_slot", "mint", "slot"),
    Index("ix_scout_events_creator_slot", "creator", "slot"),
    Index("ix_scout_events_user_slot", "user", "slot"),
    Index("ix_scout_events_pool_slot", "pool", "slot"),
    Index("ix_scout_events_type_slot", "event_type", "slot"),
    Index(
        "ix_scout_events_signature_instruction_event",
        "signature",
        "instruction_index",
        "event_index",
    ),
)

token_lifecycles = Table(
    "token_lifecycles",
    metadata,
    Column("mint", String(64), ForeignKey("tokens.mint"), primary_key=True),
    Column("state", String(24), nullable=False),
    Column("first_seen_slot", Integer),
    Column("last_seen_slot", Integer),
    Column("bonding_curve", String(64)),
    Column("pool", String(64)),
    Column("creator", String(64)),
    Column("user", String(64)),
    Column("quote_mint", String(64)),
    Column("provisional", Boolean, nullable=False, default=True),
    Column("data_gap", Boolean, nullable=False, default=False),
    Column("last_event_id", Integer, ForeignKey("scout_events.id")),
    _dt("updated_at"),
)

evidence = Table(
    "evidence",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("launch_id", Integer, ForeignKey("launches.id"), nullable=False),
    Column("category", String(24), nullable=False),
    Column("key", String(128), nullable=False),
    _json("value_json", nullable=True),
    Column("state", String(24), nullable=False),
    Column("source", Text, nullable=False),
    Column("source_endpoint", Text),
    Column("source_signature", String(88)),
    _dt("observed_at"),
    _dt("fresh_until", nullable=True),
    Column("source_slot", Integer),
    Column("error_code", String(128)),
    Column("error_message_safe", Text),
    Column("algorithm_version", String(64)),
    _dt("created_at"),
    Index(
        "ix_evidence_launch_category_key_observed", "launch_id", "category", "key", "observed_at"
    ),
)

score_snapshots = Table(
    "score_snapshots",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("launch_id", Integer, ForeignKey("launches.id"), nullable=False),
    Column("rule_version", String(64), nullable=False),
    Column("score", Integer, nullable=False),
    Column("classification", String(16), nullable=False),
    Column("decision", String(16), nullable=False),
    Column("confidence", String(16), nullable=False),
    _json("reason_json"),
    Column("evidence_hash", String(64), nullable=False),
    _dt("created_at"),
    CheckConstraint("score >= 0 AND score <= 100", name="score_snapshots_score_range"),
    Index("ix_score_snapshots_launch_created", "launch_id", "created_at"),
    Index("ix_score_snapshots_classification_created", "classification", "created_at"),
)

alerts = Table(
    "alerts",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("launch_id", Integer, ForeignKey("launches.id"), nullable=False),
    Column("channel", String(16), nullable=False),
    Column("dedupe_key", String(256), nullable=False, unique=True),
    Column("status", String(16), nullable=False),
    Column("message_text", Text, nullable=False),
    Column("provider_message_id", Text),
    Column("attempt_count", Integer, nullable=False, default=0),
    Column("last_error_safe", Text),
    _dt("created_at"),
    _dt("sent_at", nullable=True),
)

outcomes = Table(
    "outcomes",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("launch_id", Integer, ForeignKey("launches.id"), nullable=False),
    Column("horizon_minutes", Integer, nullable=False),
    _dt("observed_at"),
    Column("price_reference", Numeric),
    Column("market_cap_reference", Numeric),
    Column("volume_reference", Numeric),
    Column("liquidity_reference", Numeric),
    Column("source", Text, nullable=False),
    _dt("source_timestamp", nullable=True),
    Column("cache_age_seconds", Integer),
    Column("state", String(24), nullable=False),
    _json("raw_json", nullable=True),
    UniqueConstraint("launch_id", "horizon_minutes", name="uq_outcomes_launch_horizon"),
)

graph_nodes = Table(
    "graph_nodes",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("node_key", String(256), nullable=False, unique=True),
    Column("node_type", String(32), nullable=False),
    Column("address_or_identifier", Text, nullable=False),
    Column("network", String(16), nullable=False),
    _dt("first_seen_at"),
    _dt("last_seen_at"),
    _json("metadata_json", nullable=True),
    Column("validation_status", String(16), nullable=False),
    Index("ix_graph_nodes_type_identifier", "node_type", "address_or_identifier"),
    Index("ix_graph_nodes_network_type", "network", "node_type"),
)

graph_edges = Table(
    "graph_edges",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("source_node_id", Integer, ForeignKey("graph_nodes.id"), nullable=False),
    Column("target_node_id", Integer, ForeignKey("graph_nodes.id"), nullable=False),
    Column("edge_type", String(40), nullable=False),
    Column("direction", String(16), nullable=False),
    Column("strength", String(16), nullable=False),
    Column("status", String(16), nullable=False),
    _dt("first_observed_at"),
    _dt("last_observed_at"),
    Column("algorithm_version", String(64), nullable=False),
    UniqueConstraint(
        "source_node_id", "target_node_id", "edge_type", "direction", name="uq_graph_edges_identity"
    ),
)

graph_edge_evidence = Table(
    "graph_edge_evidence",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("edge_id", Integer, ForeignKey("graph_edges.id"), nullable=False),
    Column("evidence_type", String(64), nullable=False),
    Column("signature", String(88)),
    Column("slot", Integer),
    Column("event_key", String(64)),
    Column("source", Text, nullable=False),
    _json("details_json"),
    _dt("observed_at"),
    _dt("expires_at", nullable=True),
    Column("algorithm_version", String(64), nullable=False),
    Index("ix_graph_edge_evidence_edge_observed", "edge_id", "observed_at"),
    Index("ix_graph_edge_evidence_signature", "signature"),
    Index("ix_graph_edge_evidence_event_key", "event_key"),
)

provider_catalog_snapshots = Table(
    "provider_catalog_snapshots",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("program_id", String(64), nullable=False),
    Column("program_name", String(64)),
    Column("idl_revision", String(128), nullable=False),
    _json("payload_json"),
    Column("content_sha256", String(64), nullable=False),
    _dt("observed_at"),
    Column("source_url", Text, nullable=False),
    Column("active", Boolean, nullable=False, default=False),
    UniqueConstraint("program_id", "idl_revision", name="uq_provider_catalog_program_revision"),
)

reconciliation_records = Table(
    "reconciliation_records",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("event_key", String(64), ForeignKey("scout_events.event_key"), nullable=False),
    Column("confirmed_payload_ref", Integer, ForeignKey("raw_events.id")),
    Column("finalized_payload_ref", Integer, ForeignKey("raw_events.id")),
    Column("status", String(16), nullable=False),
    Column("reason", Text),
    Column("confirmed_slot", Integer),
    Column("finalized_slot", Integer),
    _dt("created_at"),
    _dt("resolved_at", nullable=True),
)

cursors = Table(
    "cursors",
    metadata,
    Column("name", String(128), primary_key=True),
    _json("cursor_json"),
    _dt("last_success_at", nullable=True),
    Column("last_received_slot", Integer),
    Column("last_received_signature", String(88)),
    _dt("updated_at"),
)

job_runs = Table(
    "job_runs",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("job_name", String(128), nullable=False),
    Column("status", String(24), nullable=False),
    _dt("started_at"),
    _dt("completed_at", nullable=True),
    _json("counts_json"),
    Column("safe_error_code", String(128)),
    Column("safe_error_message", Text),
    Index("ix_job_runs_name_started", "job_name", "started_at"),
)

__all__ = [
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
]
