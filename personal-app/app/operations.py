"""Local diagnostics, safe metrics, and bounded graph exports."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, case, func, inspect, or_, select

from app.config import Settings, _validate_base58_address
from app.db.graph import ALGORITHM_VERSION, utc
from app.db.models import (
    graph_edge_evidence,
    graph_edges,
    graph_nodes,
    job_runs,
    metadata,
    raw_events,
    scout_events,
    token_lifecycles,
)
from app.db.raw_journal import RawJournal, SessionSource


class Operations:
    def __init__(self, source: SessionSource, settings: Settings) -> None:
        self._source = source
        self._settings = settings

    def health(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ready": False,
            "mode": self._settings.ingest_mode,
            "network": self._settings.solana_network,
            "transport": self._settings.ingest_transport,
            "database_state": "error",
            "provider_state": "not_read",
            "ingestion_state": "not_read",
            "counts": None,
            "last_received_at": None,
            "last_run_status": None,
        }
        session, owned = RawJournal(self._source)._session()
        try:
            inspector = inspect(session.get_bind())
            if not set(metadata.tables) <= set(inspector.get_table_names()) or any(
                not {column.name for column in table.columns}
                <= {column["name"] for column in inspector.get_columns(table.name)}
                for table in metadata.sorted_tables
            ):
                result["database_state"] = "schema_unavailable"
                return result
            result["counts"] = {
                table.name: session.scalar(select(func.count()).select_from(table))
                for table in (
                    raw_events,
                    scout_events,
                    token_lifecycles,
                    graph_nodes,
                    graph_edges,
                    job_runs,
                )
            }
            last_received = session.scalar(select(func.max(raw_events.c.received_at)))
            result["last_received_at"] = utc(last_received).isoformat() if last_received else None
            result["last_run_status"] = session.scalar(
                select(job_runs.c.status).order_by(job_runs.c.started_at.desc()).limit(1)
            )
            result["ready"] = True
            result["database_state"] = "observed"
            return result
        except Exception:
            return result
        finally:
            if owned:
                session.close()

    def metrics(self) -> str:
        health = self.health()
        lines = [
            "# TYPE quantumorb_database_ready gauge",
            f"quantumorb_database_ready {int(health['ready'])}",
        ]
        if health["counts"] is not None:
            for name, count in sorted(health["counts"].items()):
                metric = f"quantumorb_{name}_rows"
                lines.extend((f"# TYPE {metric} gauge", f"{metric} {count}"))
        return "\n".join(lines) + "\n"

    def export_graph(
        self,
        *,
        mint: str | None = None,
        limit: int = 1000,
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 5000:
            raise ValueError("graph export limit is invalid")
        if mint is not None:
            _validate_base58_address(mint)
        now = observed_at or datetime.now(UTC)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("graph export time must be aware")
        session, owned = RawJournal(self._source)._session()
        try:
            query = (
                select(graph_edges)
                .join(graph_nodes, graph_edges.c.source_node_id == graph_nodes.c.id)
                .where(graph_nodes.c.network == self._settings.solana_network)
            )
            if mint is not None:
                matching = select(graph_edge_evidence.c.edge_id).where(
                    func.json_extract(
                        graph_edge_evidence.c.details_json, "$.observation.details.mint"
                    )
                    == mint
                )
                query = query.where(graph_edges.c.id.in_(matching))
            rows = (
                session.execute(query.order_by(graph_edges.c.id).limit(limit + 1)).mappings().all()
            )
            truncated = len(rows) > limit
            rows = rows[:limit]
            node_ids = {
                value for row in rows for value in (row["source_node_id"], row["target_node_id"])
            }
            nodes = (
                session.execute(
                    select(graph_nodes)
                    .where(graph_nodes.c.id.in_(node_ids))
                    .order_by(graph_nodes.c.id)
                )
                .mappings()
                .all()
                if node_ids
                else []
            )
            edge_ids = [row["id"] for row in rows]
            fresh = and_(
                or_(
                    graph_edge_evidence.c.expires_at.is_(None),
                    graph_edge_evidence.c.expires_at > now,
                ),
                func.json_extract(graph_edge_evidence.c.details_json, "$.observation.status").in_(
                    ["active", "needs_review"]
                ),
            )
            evidence = {
                row.edge_id: (row.total, row.fresh)
                for row in session.execute(
                    select(
                        graph_edge_evidence.c.edge_id,
                        func.count().label("total"),
                        func.sum(case((fresh, 1), else_=0)).label("fresh"),
                    )
                    .where(graph_edge_evidence.c.edge_id.in_(edge_ids))
                    .group_by(graph_edge_evidence.c.edge_id)
                )
            }
            exported_edges = []
            for row in rows:
                total, fresh_count = evidence.get(row["id"], (0, 0))
                state = "observed" if fresh_count else "stale" if total else "not_read"
                status = row["status"]
                if status == "retracted":
                    state = "not_applicable"
                elif state == "stale":
                    status = "expired"
                exported_edges.append(
                    {
                        "id": row["id"],
                        "source_node_id": row["source_node_id"],
                        "target_node_id": row["target_node_id"],
                        "edge_type": row["edge_type"],
                        "direction": row["direction"],
                        "strength": row["strength"],
                        "status": status,
                        "evidence_state": state,
                        "evidence_count": total,
                        "algorithm_version": ALGORITHM_VERSION
                        if row["algorithm_version"] == ALGORITHM_VERSION
                        else "unknown",
                    }
                )
            return {
                "scope": "relationships",
                "mint": mint,
                "observed_at": now.isoformat(),
                "state": "observed",
                "redacted": True,
                "truncated": truncated,
                "limits": {"max_edges": limit, "max_nodes": 2 * limit},
                "nodes": [
                    {
                        key: row[key]
                        for key in (
                            "id",
                            "node_key",
                            "node_type",
                            "address_or_identifier",
                            "network",
                        )
                    }
                    for row in nodes
                ],
                "edges": exported_edges,
            }
        finally:
            if owned:
                session.close()
