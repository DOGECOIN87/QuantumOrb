"""Scoped transaction and slot evidence without inferred bundle identities."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations
from typing import Any

from sqlalchemy import select

from app.config import Settings, _validate_base58_address
from app.db.graph import ALGORITHM_VERSION, GraphRepository, make_node
from app.db.models import scout_events
from app.db.raw_journal import SessionSource
from app.domain.enums import (
    GraphEdgeDirection,
    GraphEdgeStatus,
    GraphEdgeStrength,
    GraphEdgeType,
    GraphNodeType,
)
from app.domain.models import GraphEdgeEvidence
from app.funding import verify_history
from app.integrations.wallet_history import WalletHistoryResult
from app.transfers import TransferExtractor


@dataclass
class _Transaction:
    slot: int
    roles: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    raw_ids: set[int] = field(default_factory=set)
    event_keys: set[str] = field(default_factory=set)
    provisional: bool = False


@dataclass(frozen=True)
class TemporalResult:
    shared_transaction_groups: int
    same_slot_groups: int
    same_bundle_groups: None
    warnings: tuple[str, ...]
    complete: bool
    nodes_added: int
    edges_added: int
    events_scanned: int


class TemporalDetector:
    def __init__(self, source: SessionSource, settings: Settings) -> None:
        self._repo = GraphRepository(source)
        self._settings = Settings.model_validate(settings.model_dump())

    def analyze(
        self,
        mint: str,
        *,
        observed_at: datetime,
        histories: Sequence[WalletHistoryResult] = (),
    ) -> TemporalResult:
        _validate_base58_address(mint)
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("temporal observation requires aware time")
        if self._settings.solana_network != "mainnet-beta":
            raise ValueError("temporal network context is unavailable")
        if len(histories) > self._settings.cartographer_max_wallets_per_token:
            raise ValueError("temporal history scope exceeds configured limits")
        warnings = {"bundle_data_unavailable"}
        groups: dict[str, _Transaction] = {}
        seen_wallets: set[str] = set()

        def add(group: _Transaction, wallet: str, role: dict[str, Any]) -> None:
            try:
                _validate_base58_address(wallet)
            except ValueError:
                warnings.add("temporal_address_invalid")
                return
            if wallet not in seen_wallets and len(seen_wallets) >= (
                self._settings.cartographer_max_wallets_per_token
            ):
                warnings.add("wallet_limit_reached")
                return
            seen_wallets.add(wallet)
            roles = group.roles.setdefault(wallet, [])
            if role not in roles:
                roles.append(role)

        with self._repo.transaction() as session:
            rows = (
                session.execute(
                    select(scout_events)
                    .where(
                        scout_events.c.mint == mint,
                        scout_events.c.decode_status == "decoded",
                        scout_events.c.finality_status != "reorged_or_changed",
                    )
                    .order_by(
                        scout_events.c.slot,
                        scout_events.c.transaction_index,
                        scout_events.c.instruction_index,
                        scout_events.c.event_index,
                    )
                    .limit(self._settings.cartographer_max_history_tx + 1)
                )
                .mappings()
                .all()
            )
            if len(rows) > self._settings.cartographer_max_history_tx:
                warnings.add("temporal_event_limit_reached")
                rows = rows[: self._settings.cartographer_max_history_tx]
            if not rows:
                warnings.add("token_activity_not_read")
            bad_signatures: set[str] = set()
            for row in rows:
                group = groups.setdefault(row["signature"], _Transaction(row["slot"]))
                if group.slot != row["slot"]:
                    bad_signatures.add(row["signature"])
                    warnings.add("transaction_context_conflict")
                    continue
                group.event_keys.add(row["event_key"])
                group.raw_ids.add(row["raw_event_id"])
                group.provisional |= row["finality_status"] == "provisional"
                if group.provisional:
                    warnings.add("provisional_token_activity")
                for role in ("user", "creator"):
                    if row[role] is not None:
                        add(
                            group,
                            row[role],
                            {
                                "role": role,
                                "direct": True,
                                "signature": row["signature"],
                                "instruction_index": row["instruction_index"],
                                "event_key": row["event_key"],
                            },
                        )
            for history in histories:
                verify_history(session, history)
                warnings.update(history.coverage.warnings)
                warnings.update(history.coverage.incomplete_reasons)
                if not history.coverage.history_complete:
                    warnings.add("temporal_history_incomplete")
                for item in history.transactions:
                    history_group = groups.get(item.signature)
                    if history_group is None:
                        continue
                    group = history_group
                    if group.slot != item.slot:
                        bad_signatures.add(item.signature)
                        warnings.add("transaction_context_conflict")
                        continue
                    extraction = TransferExtractor().extract(item)
                    warnings.update(extraction.warnings)
                    for fact in extraction.transfers:
                        if fact.context != "transfer":
                            continue
                        group.raw_ids.add(fact.raw_event_id)
                        for role, wallet in (
                            (
                                "transfer_source",
                                fact.source_account if fact.asset == "sol" else fact.source_owner,
                            ),
                            (
                                "transfer_destination",
                                fact.destination_account
                                if fact.asset == "sol"
                                else fact.destination_owner,
                            ),
                        ):
                            if wallet is None:
                                warnings.add("temporal_owner_unavailable")
                            else:
                                add(
                                    group,
                                    wallet,
                                    {
                                        "role": role,
                                        "direct": False,
                                        "signature": fact.signature,
                                        "instruction_index": fact.top_level_instruction_index,
                                        "inner_instruction_index": fact.inner_instruction_index,
                                    },
                                )
            for signature in bad_signatures:
                groups.pop(signature, None)
            repo = GraphRepository(session)
            nodes_added = edges_added = emitted = 0
            shared_groups: set[str] = set()
            slot_groups: set[int] = set()
            counterparties: dict[str, set[str]] = {}

            def record(
                left: str,
                right: str,
                observations: list[tuple[str, _Transaction]],
                edge_type: GraphEdgeType,
            ) -> bool:
                nonlocal nodes_added, edges_added, emitted
                if emitted >= self._settings.cartographer_max_temporal_edges:
                    warnings.add("temporal_edge_limit_reached")
                    return False
                for a, b in ((left, right), (right, left)):
                    peers = counterparties.setdefault(a, set())
                    if (
                        b not in peers
                        and len(peers) >= self._settings.cartographer_max_counterparties_per_wallet
                    ):
                        warnings.add("counterparty_limit_reached")
                        return False
                counterparties[left].add(right)
                counterparties[right].add(left)
                same_tx = edge_type == GraphEdgeType.SHARED_TRANSACTION
                group = observations[0][1]
                roles = {
                    wallet: [
                        role for _, item in observations for role in item.roles.get(wallet, [])
                    ]
                    for wallet in (left, right)
                }
                strength = GraphEdgeStrength.WEAK
                if same_tx:
                    strength = (
                        GraphEdgeStrength.DIRECT
                        if all(
                            any(role["direct"] for role in roles[wallet])
                            for wallet in (left, right)
                        )
                        else GraphEdgeStrength.MODERATE
                    )
                provisional = any(item.provisional for _, item in observations)
                details: dict[str, Any] = {
                    "mint": mint,
                    "roles": roles,
                    "signatures": sorted({signature for signature, _ in observations}),
                    "raw_event_ids": sorted(
                        {ref for _, item in observations for ref in item.raw_ids}
                    ),
                    "event_keys": sorted(
                        {key for _, item in observations for key in item.event_keys}
                    ),
                    "provisional": provisional,
                }
                write = repo.record_relationship(
                    make_node(GraphNodeType.WALLET, left, observed_at),
                    make_node(GraphNodeType.WALLET, right, observed_at),
                    GraphEdgeEvidence(
                        edge_type=edge_type,
                        strength=strength,
                        evidence_type=edge_type.value,
                        source="canonical_scout_and_history",
                        signature=observations[0][0] if same_tx else None,
                        slot=group.slot,
                        event_key=sorted(group.event_keys)[0],
                        details=details,
                        observed_at=observed_at,
                        algorithm_version=ALGORITHM_VERSION,
                    ),
                    direction=GraphEdgeDirection.UNDIRECTED,
                    status=GraphEdgeStatus.NEEDS_REVIEW if provisional else GraphEdgeStatus.ACTIVE,
                )
                nodes_added += write.nodes_added
                edges_added += write.edges_added
                emitted += 1
                return True

            for signature, group in sorted(groups.items()):
                for left, right in combinations(sorted(group.roles), 2):
                    if record(left, right, [(signature, group)], GraphEdgeType.SHARED_TRANSACTION):
                        shared_groups.add(signature)
                    if "temporal_edge_limit_reached" in warnings:
                        break
                if "temporal_edge_limit_reached" in warnings:
                    break
            by_slot: dict[int, list[tuple[str, _Transaction]]] = {}
            for signature, group in groups.items():
                by_slot.setdefault(group.slot, []).append((signature, group))
            for slot, observations in sorted(by_slot.items()):
                wallets = sorted({wallet for _, group in observations for wallet in group.roles})
                for left, right in combinations(wallets, 2):
                    relevant = [
                        (sig, group)
                        for sig, group in observations
                        if left in group.roles or right in group.roles
                    ]
                    left_sigs = {sig for sig, group in relevant if left in group.roles}
                    right_sigs = {sig for sig, group in relevant if right in group.roles}
                    if not any(a != b for a in left_sigs for b in right_sigs):
                        continue
                    if record(left, right, relevant, GraphEdgeType.SAME_SLOT_ACTIVITY):
                        slot_groups.add(slot)
                    if "temporal_edge_limit_reached" in warnings:
                        break
                if "temporal_edge_limit_reached" in warnings:
                    break
            return TemporalResult(
                len(shared_groups),
                len(slot_groups),
                None,
                tuple(sorted(warnings)),
                not (warnings - {"bundle_data_unavailable"}),
                nodes_added,
                edges_added,
                len(rows),
            )
