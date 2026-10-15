"""Typed graph persistence with immutable evidence and replay-safe identities."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from app.config import _validate_base58_address
from app.db.models import graph_edge_evidence, graph_edges, graph_nodes, raw_events, scout_events
from app.db.raw_journal import RawJournal, SessionSource
from app.domain.enums import (
    GraphEdgeDirection,
    GraphEdgeStatus,
    GraphEdgeStrength,
    GraphEdgeType,
    GraphNodeType,
    Network,
    NodeValidationStatus,
)
from app.domain.models import GraphEdge, GraphEdgeEvidence, GraphNode, JsonObject, _signature

ALGORITHM_VERSION = "cartographer-0.1.0"


class GraphError(ValueError):
    def __init__(self, code: str = "invalid_graph_observation") -> None:
        self.code = code
        super().__init__("graph observation could not be persisted")


def utc(value: datetime) -> datetime:
    """Restore SQLite's UTC convention on reads; inputs validate awareness first."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _node_input(value: GraphNode) -> GraphNode:
    try:
        node = GraphNode.model_validate(value.model_dump())
        kind, identifier = node.node_type, node.address_or_identifier
        if kind == GraphNodeType.TRANSACTION:
            _signature(identifier)
        elif kind == GraphNodeType.METADATA_URI:
            if len(identifier) != 64 or any(c not in "0123456789abcdef" for c in identifier):
                raise ValueError
        elif kind == GraphNodeType.BUNDLE:
            raise GraphError("bundle_data_unavailable")
        else:
            _validate_base58_address(identifier)
        if (
            node.node_key != f"{kind.value}:{identifier}"
            or node.last_seen_at < node.first_seen_at
            or node.validation_status != NodeValidationStatus.VALID
        ):
            raise ValueError
        _json(node.metadata)
        return node
    except GraphError:
        raise
    except Exception:
        raise GraphError() from None


def make_node(
    kind: GraphNodeType,
    identifier: str,
    observed_at: datetime,
    *,
    network: Network = Network.MAINNET_BETA,
    metadata: JsonObject | None = None,
) -> GraphNode:
    return _node_input(
        GraphNode(
            node_key=f"{kind.value}:{identifier}",
            node_type=kind,
            address_or_identifier=identifier,
            network=network,
            first_seen_at=observed_at,
            last_seen_at=observed_at,
            metadata=metadata,
            validation_status=NodeValidationStatus.VALID,
        )
    )


def _node(row: Any) -> GraphNode:
    data = dict(row._mapping if hasattr(row, "_mapping") else row)
    metadata_json = data.pop("metadata_json")
    data["metadata"] = json.loads(metadata_json) if metadata_json is not None else None
    for name in ("first_seen_at", "last_seen_at"):
        data[name] = utc(data[name])
    return GraphNode.model_validate(data)


def _edge(row: Any) -> GraphEdge:
    data = dict(row._mapping if hasattr(row, "_mapping") else row)
    for name in ("first_observed_at", "last_observed_at"):
        data[name] = utc(data[name])
    return GraphEdge.model_validate(data)


@dataclass(frozen=True)
class GraphWrite:
    source: GraphNode
    target: GraphNode
    edge: GraphEdge
    evidence_id: int
    nodes_added: int
    edges_added: int
    evidence_added: bool


class GraphRepository:
    def __init__(self, source: SessionSource) -> None:
        self._source = source

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        """Serialize SQLite writes and preserve a caller's transaction ownership."""
        session, owned = RawJournal(self._source)._session()
        try:
            if owned:
                with session.begin():
                    self._lock_sqlite(session)
                    yield session
            else:
                self._lock_sqlite(session)
                with session.begin_nested():
                    yield session
        finally:
            if owned:
                session.close()

    @staticmethod
    def _lock_sqlite(session: Session) -> None:
        connection = session.connection()
        if connection.dialect.name == "sqlite":
            dbapi = connection.connection.driver_connection
            if not getattr(dbapi, "in_transaction", False):
                connection.exec_driver_sql("BEGIN IMMEDIATE")

    @staticmethod
    def _upsert(session: Session, node: GraphNode) -> tuple[GraphNode, bool]:
        existing = session.execute(
            select(graph_nodes).where(graph_nodes.c.node_key == node.node_key).with_for_update()
        ).first()
        added = existing is None
        if existing is None:
            session.execute(
                insert(graph_nodes).values(
                    node_key=node.node_key,
                    node_type=node.node_type.value,
                    address_or_identifier=node.address_or_identifier,
                    network=node.network.value,
                    first_seen_at=node.first_seen_at,
                    last_seen_at=node.last_seen_at,
                    metadata_json=_json(node.metadata) if node.metadata is not None else None,
                    validation_status=node.validation_status.value,
                )
            )
        else:
            stored = _node(existing)
            if (
                stored.network != node.network
                or stored.node_type != node.node_type
                or stored.address_or_identifier != node.address_or_identifier
                or (node.id is not None and node.id != stored.id)
            ):
                raise GraphError("graph_node_identity_conflict")
            # The first metadata observation is retained; graph evidence carries
            # later observations. Replaying a node never erases metadata.
            session.execute(
                update(graph_nodes)
                .where(graph_nodes.c.id == stored.id)
                .values(
                    first_seen_at=min(stored.first_seen_at, node.first_seen_at),
                    last_seen_at=max(stored.last_seen_at, node.last_seen_at),
                )
            )
        row = session.execute(
            select(graph_nodes).where(graph_nodes.c.node_key == node.node_key)
        ).one()
        return _node(row), added

    def upsert_node(self, node: GraphNode) -> GraphNode:
        node = _node_input(node)
        with self.transaction() as session:
            return self._upsert(session, node)[0]

    @staticmethod
    def _provenance(session: Session, evidence: GraphEdgeEvidence) -> None:
        refs = evidence.details.get("raw_event_ids", [])
        if not isinstance(refs, list) or any(type(ref) is not int or ref <= 0 for ref in refs):
            raise GraphError("graph_provenance_invalid")
        known = False
        for ref in refs:
            row = session.execute(select(raw_events).where(raw_events.c.id == ref)).first()
            if row is None:
                raise GraphError("graph_provenance_missing")
            raw = row._mapping
            if evidence.signature is not None and raw["signature"] != evidence.signature:
                raise GraphError("graph_provenance_conflict")
            if evidence.slot is not None and raw["slot"] != evidence.slot:
                raise GraphError("graph_provenance_conflict")
            known = True
        if evidence.event_key is not None:
            event = session.execute(
                select(scout_events).where(scout_events.c.event_key == evidence.event_key)
            ).first()
            if event is None:
                raise GraphError("graph_provenance_missing")
            if (
                evidence.signature is not None and event._mapping["signature"] != evidence.signature
            ) or (evidence.slot is not None and event._mapping["slot"] != evidence.slot):
                raise GraphError("graph_provenance_conflict")
            known = True
        if not known:
            raise GraphError("graph_provenance_missing")

    def record_relationship(
        self,
        source: GraphNode,
        target: GraphNode,
        evidence: GraphEdgeEvidence,
        *,
        direction: GraphEdgeDirection = GraphEdgeDirection.DIRECTED,
        status: GraphEdgeStatus = GraphEdgeStatus.ACTIVE,
    ) -> GraphWrite:
        source, target = _node_input(source), _node_input(target)
        try:
            evidence = GraphEdgeEvidence.model_validate(evidence.model_dump())
            direction, status = GraphEdgeDirection(direction), GraphEdgeStatus(status)
            if (
                source.network != target.network
                or source.node_key == target.node_key
                or not evidence.source
                or not evidence.evidence_type
                or not evidence.algorithm_version
                or len(evidence.source) > 256
                or len(evidence.evidence_type) > 64
                or len(evidence.algorithm_version) > 64
                or (evidence.expires_at is not None and evidence.expires_at <= evidence.observed_at)
            ):
                raise ValueError
            if evidence.edge_type == GraphEdgeType.SAME_SLOT_ACTIVITY and (
                evidence.strength != GraphEdgeStrength.WEAK
            ):
                raise GraphError("same_slot_requires_weak_strength")
            if evidence.edge_type == GraphEdgeType.SAME_BUNDLE_ACTIVITY:
                raise GraphError("bundle_data_unavailable")
            body = evidence.model_dump(mode="json", exclude={"observed_at"})
            body["status"] = status.value
            identity = hashlib.sha256(_json(body).encode()).hexdigest()
            retained = _json({"identity": identity, "observation": body})
        except GraphError:
            raise
        except Exception:
            raise GraphError() from None
        if direction == GraphEdgeDirection.UNDIRECTED and source.node_key > target.node_key:
            source, target = target, source
        with self.transaction() as session:
            self._provenance(session, evidence)
            source, source_added = self._upsert(session, source)
            target, target_added = self._upsert(session, target)
            criteria = (
                (graph_edges.c.source_node_id == source.id)
                & (graph_edges.c.target_node_id == target.id)
                & (graph_edges.c.edge_type == evidence.edge_type.value)
                & (graph_edges.c.direction == direction.value)
            )
            row = session.execute(select(graph_edges).where(criteria).with_for_update()).first()
            added = row is None
            if row is None:
                session.execute(
                    insert(graph_edges).values(
                        source_node_id=source.id,
                        target_node_id=target.id,
                        edge_type=evidence.edge_type.value,
                        direction=direction.value,
                        strength=evidence.strength.value,
                        status=status.value,
                        first_observed_at=evidence.observed_at,
                        last_observed_at=evidence.observed_at,
                        algorithm_version=evidence.algorithm_version,
                    )
                )
                row = session.execute(select(graph_edges).where(criteria)).one()
            edge = _edge(row)
            duplicate = session.execute(
                select(graph_edge_evidence.c.id).where(
                    graph_edge_evidence.c.edge_id == edge.id,
                    graph_edge_evidence.c.details_json == retained,
                    graph_edge_evidence.c.evidence_type == evidence.evidence_type,
                    graph_edge_evidence.c.source == evidence.source,
                    graph_edge_evidence.c.algorithm_version == evidence.algorithm_version,
                )
            ).first()
            if duplicate:
                evidence_id = int(duplicate.id)
            else:
                evidence_id = session.execute(
                    insert(graph_edge_evidence)
                    .values(
                        edge_id=edge.id,
                        evidence_type=evidence.evidence_type,
                        signature=evidence.signature,
                        slot=evidence.slot,
                        event_key=evidence.event_key,
                        source=evidence.source,
                        details_json=retained,
                        observed_at=evidence.observed_at,
                        expires_at=evidence.expires_at,
                        algorithm_version=evidence.algorithm_version,
                    )
                    .returning(graph_edge_evidence.c.id)
                ).scalar_one()
                latest = evidence.observed_at >= edge.last_observed_at
                session.execute(
                    update(graph_edges)
                    .where(graph_edges.c.id == edge.id)
                    .values(
                        first_observed_at=min(edge.first_observed_at, evidence.observed_at),
                        last_observed_at=max(edge.last_observed_at, evidence.observed_at),
                        strength=evidence.strength.value if latest else edge.strength.value,
                        status=status.value if latest else edge.status.value,
                        algorithm_version=evidence.algorithm_version
                        if latest
                        else edge.algorithm_version,
                    )
                )
                edge = _edge(session.execute(select(graph_edges).where(criteria)).one())
            return GraphWrite(
                source,
                target,
                edge,
                evidence_id,
                int(source_added) + int(target_added),
                int(added),
                duplicate is None,
            )

    def nodes(self, *, network: Network = Network.MAINNET_BETA) -> tuple[GraphNode, ...]:
        session, owned = RawJournal(self._source)._session()
        try:
            return tuple(
                _node(row)
                for row in session.execute(
                    select(graph_nodes)
                    .where(graph_nodes.c.network == network.value)
                    .order_by(graph_nodes.c.node_key)
                )
            )
        finally:
            if owned:
                session.close()

    def edges(self, *, network: Network = Network.MAINNET_BETA) -> tuple[GraphEdge, ...]:
        session, owned = RawJournal(self._source)._session()
        try:
            return tuple(
                _edge(row)
                for row in session.execute(
                    select(graph_edges)
                    .join(graph_nodes, graph_edges.c.source_node_id == graph_nodes.c.id)
                    .where(graph_nodes.c.network == network.value)
                    .order_by(graph_edges.c.id)
                )
            )
        finally:
            if owned:
                session.close()
