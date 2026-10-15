"""Persisted, bounded Cartographer summaries and explicit history backfills."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from pydantic import Field, field_validator
from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session

from app.config import Settings, _validate_base58_address
from app.db.graph import ALGORITHM_VERSION, GraphRepository, make_node
from app.db.models import job_runs, scout_events
from app.db.raw_journal import RawJournal, SessionSource
from app.domain.enums import (
    EvidenceState,
    GraphEdgeStatus,
    GraphEdgeStrength,
    GraphEdgeType,
    GraphNodeType,
)
from app.domain.models import (
    CartographerCoverage,
    CartographerEvidenceUpdated,
    GraphEdgeEvidence,
    UtcModel,
)
from app.funding import FundingDetector, FundingError, FundingLink, first_activity
from app.integrations.wallet_history import WalletHistoryAdapter, WalletHistoryResult
from app.integrations.wallet_history_models import HistoryQuery
from app.temporal import TemporalDetector


class CartographerError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("Cartographer operation could not be completed")


class SummaryCoverage(CartographerCoverage):
    provider: str = "helius_getTransactionsForAddress"
    scope: str = "persisted_canonical_events_and_bounded_wallet_history"
    missing_wallet_histories: int = Field(ge=0)
    limits: dict[str, int]
    warnings: list[str]


class CartographerSummary(UtcModel):
    run_id: str
    mint: str
    creator_wallet: str | None
    user_wallet: str | None
    trader_count_observed: int | None = Field(ge=0)
    wallets_with_inbound_funding: int | None = Field(ge=0)
    shared_funder_groups: int | None = Field(ge=0)
    same_transaction_groups: int | None = Field(ge=0)
    same_slot_groups: int | None = Field(ge=0)
    same_bundle_groups: None = None
    coverage: SummaryCoverage
    warnings: list[str]
    observed_at: datetime
    state: EvidenceState
    algorithm_version: str = ALGORITHM_VERSION

    _mint = field_validator("mint")(_validate_base58_address)


@dataclass(frozen=True)
class AnalysisResult:
    summary: CartographerSummary
    message: CartographerEvidenceUpdated


class CartographerService:
    def __init__(self, source: SessionSource, settings: Settings) -> None:
        self._source = source
        self._settings = Settings.model_validate(settings.model_dump())

    def limits(self) -> dict[str, int]:
        settings = self._settings
        return {
            "lookback_days": settings.cartographer_lookback_days,
            "max_wallets": settings.cartographer_max_wallets_per_token,
            "max_traders": settings.cartographer_max_traders_per_token,
            "max_history_transactions": settings.cartographer_max_history_tx,
            "max_counterparties": settings.cartographer_max_counterparties_per_wallet,
            "max_temporal_edges": settings.cartographer_max_temporal_edges,
            "min_funding_lamports": settings.cartographer_min_funding_lamports,
            "near_time_seconds": settings.cartographer_near_time_seconds,
        }

    def _scope(
        self, session: Session, mint: str
    ) -> tuple[list[Any], list[str], set[str], set[str]]:
        warnings: set[str] = set()
        rows = list(
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
            ).mappings()
        )
        if len(rows) > self._settings.cartographer_max_history_tx:
            rows = rows[: self._settings.cartographer_max_history_tx]
            warnings.add("scout_event_limit_reached")
        candidates: list[str] = []
        traders: set[str] = set()
        for row in rows:
            if row["event_type"] == "token_created":
                candidates.extend(wallet for wallet in (row["creator"], row["user"]) if wallet)
        for row in rows:
            if (
                row["event_type"] in {"curve_buy", "curve_sell", "pool_buy", "pool_sell"}
                and row["user"]
            ):
                wallet = row["user"]
                if wallet not in traders:
                    traders.add(wallet)
                    if len(traders) <= self._settings.cartographer_max_traders_per_token:
                        candidates.append(wallet)
                    else:
                        warnings.add("trader_limit_reached")
        candidates.extend(row["creator"] for row in rows if row["creator"])
        candidates.extend(
            row["user"] for row in rows if row["event_type"] == "pool_created" and row["user"]
        )
        wallets: list[str] = []
        for wallet in dict.fromkeys(candidates):
            try:
                _validate_base58_address(wallet)
            except ValueError:
                warnings.add("scope_address_invalid")
                continue
            if len(wallets) >= self._settings.cartographer_max_wallets_per_token:
                warnings.add("wallet_limit_reached")
                break
            wallets.append(wallet)
        return rows, wallets, traders, warnings

    @staticmethod
    def _project(
        session: Session, rows: list[Any], mint: str, observed_at: datetime
    ) -> tuple[int, int]:
        repo = GraphRepository(session)
        nodes = edges = 0
        for row in rows:
            relationships: list[
                tuple[GraphNodeType, str, GraphNodeType, str, GraphEdgeType, str]
            ] = []
            if row["event_type"] == "token_created":
                for role, edge_type in (
                    ("creator", GraphEdgeType.CREATOR_DECLARED_FOR_TOKEN),
                    ("user", GraphEdgeType.USER_CREATED_TOKEN),
                ):
                    if row[role]:
                        relationships.append(
                            (
                                GraphNodeType.WALLET,
                                row[role],
                                GraphNodeType.TOKEN_MINT,
                                mint,
                                edge_type,
                                role,
                            )
                        )
            elif (
                row["event_type"] in {"curve_buy", "curve_sell", "pool_buy", "pool_sell"}
                and row["user"]
            ):
                relationships.append(
                    (
                        GraphNodeType.WALLET,
                        row["user"],
                        GraphNodeType.TOKEN_MINT,
                        mint,
                        GraphEdgeType.TRADED_TOKEN,
                        "user",
                    )
                )
            if row["pool"]:
                relationships.append(
                    (
                        GraphNodeType.TOKEN_MINT,
                        mint,
                        GraphNodeType.POOL,
                        row["pool"],
                        GraphEdgeType.ASSOCIATED_WITH_POOL,
                        "pool",
                    )
                )
            for source_kind, source, target_kind, target, edge_type, role in relationships:
                details: dict[str, Any] = {
                    "mint": mint,
                    "role": role,
                    "event_type": row["event_type"],
                    "instruction_index": row["instruction_index"],
                    "raw_event_ids": [row["raw_event_id"]],
                    "finality": row["finality_status"],
                }
                write = repo.record_relationship(
                    make_node(source_kind, source, observed_at),
                    make_node(target_kind, target, observed_at),
                    GraphEdgeEvidence(
                        edge_type=edge_type,
                        strength=GraphEdgeStrength.DIRECT,
                        evidence_type=role,
                        source="canonical_scout",
                        signature=row["signature"],
                        slot=row["slot"],
                        event_key=row["event_key"],
                        details=details,
                        observed_at=observed_at,
                        algorithm_version=ALGORITHM_VERSION,
                    ),
                    status=GraphEdgeStatus.NEEDS_REVIEW
                    if row["finality_status"] == "provisional"
                    else GraphEdgeStatus.ACTIVE,
                )
                nodes += write.nodes_added
                edges += write.edges_added
        return nodes, edges

    def analyze(
        self,
        mint: str,
        histories: Mapping[str, WalletHistoryResult] | None = None,
        *,
        observed_at: datetime,
        extra_warnings: tuple[str, ...] = (),
    ) -> AnalysisResult:
        _validate_base58_address(mint)
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise CartographerError("cartographer_time_invalid")
        if self._settings.solana_network != "mainnet-beta":
            raise CartographerError("cartographer_network_unavailable")
        histories = histories or {}
        if len(histories) > self._settings.cartographer_max_wallets_per_token:
            raise CartographerError("cartographer_history_scope_limit")
        run_id = str(uuid4())
        started = datetime.now(UTC)
        try:
            with GraphRepository(self._source).transaction() as session:
                rows, wallets, traders, warnings = self._scope(session, mint)
                warnings.update(extra_warnings)
                if not rows:
                    warnings.add("token_activity_not_read")
                creates = [row for row in rows if row["event_type"] == "token_created"]
                if not creates:
                    warnings.add("create_event_not_read")
                roles: dict[str, str | None] = {}
                for role in ("creator", "user"):
                    values = {row[role] for row in creates if row[role] is not None}
                    roles[role] = next(iter(values)) if len(values) == 1 else None
                    if len(values) > 1:
                        warnings.add("create_context_conflict")
                nodes_added, edges_added = self._project(session, rows, mint, observed_at)
                selected: list[WalletHistoryResult] = []
                links: list[FundingLink] = []
                complete = bool(wallets)
                scanned = 0
                for wallet in wallets:
                    history = histories.get(wallet)
                    if history is None:
                        warnings.add("wallet_history_not_read")
                        complete = False
                        continue
                    try:
                        funding = FundingDetector(session, self._settings).analyze(
                            mint, wallet, history, observed_at=observed_at
                        )
                    except FundingError as error:
                        warnings.add(error.code)
                        complete = False
                        continue
                    selected.append(history)
                    scanned += funding.transactions_scanned
                    links.extend(funding.links)
                    warnings.update(funding.warnings)
                    complete &= funding.history_complete
                    nodes_added += funding.nodes_added
                    edges_added += funding.edges_added
                if set(histories) - set(wallets):
                    warnings.add("out_of_scope_history_ignored")
                temporal = TemporalDetector(session, self._settings).analyze(
                    mint, observed_at=observed_at, histories=selected
                )
                warnings.update(temporal.warnings)
                complete &= temporal.complete
                complete &= not bool(
                    warnings
                    & {
                        "wallet_limit_reached",
                        "trader_limit_reached",
                        "scout_event_limit_reached",
                        "create_context_conflict",
                        "scope_address_invalid",
                    }
                )
                nodes_added += temporal.nodes_added
                edges_added += temporal.edges_added
                groups: dict[str, list[tuple[int, str]]] = {}
                unknown_time = False
                for link in links:
                    if link.fact.block_time is None:
                        unknown_time = True
                    else:
                        groups.setdefault(link.source_wallet, []).append(
                            (link.fact.block_time, link.target_wallet)
                        )
                shared_funders = 0
                for entries in groups.values():
                    entries.sort()
                    left = 0
                    found = False
                    for right, (timestamp, _) in enumerate(entries):
                        while (
                            timestamp - entries[left][0]
                            > self._settings.cartographer_near_time_seconds
                        ):
                            left += 1
                        if len({wallet for _, wallet in entries[left : right + 1]}) > 1:
                            found = True
                            break
                    shared_funders += int(found)
                if unknown_time:
                    warnings.add("funding_group_time_unavailable")
                ordered_warnings = sorted(warnings)
                coverage = SummaryCoverage(
                    history_complete=complete,
                    wallets_scanned=len(selected),
                    transactions_scanned=scanned,
                    missing_wallet_histories=len(wallets) - len(selected),
                    limits=self.limits(),
                    warnings=ordered_warnings,
                )
                summary = CartographerSummary(
                    run_id=run_id,
                    mint=mint,
                    creator_wallet=roles["creator"],
                    user_wallet=roles["user"],
                    trader_count_observed=len(traders) if rows else None,
                    wallets_with_inbound_funding=len({link.target_wallet for link in links})
                    if selected
                    else None,
                    shared_funder_groups=shared_funders if selected and not unknown_time else None,
                    same_transaction_groups=temporal.shared_transaction_groups if rows else None,
                    same_slot_groups=temporal.same_slot_groups if rows else None,
                    coverage=coverage,
                    warnings=ordered_warnings,
                    observed_at=observed_at,
                    state=EvidenceState.OBSERVED if rows else EvidenceState.NOT_READ,
                )
                message = CartographerEvidenceUpdated(
                    type="CartographerEvidenceUpdated",
                    schema_version="0.1",
                    run_id=run_id,
                    summary_ref=run_id,
                    mint=mint,
                    nodes_added=nodes_added,
                    edges_added=edges_added,
                    edges_retracted=0,
                    coverage=CartographerCoverage(
                        history_complete=complete,
                        wallets_scanned=len(selected),
                        transactions_scanned=scanned,
                    ),
                    warnings=ordered_warnings,
                    algorithm_version=ALGORITHM_VERSION,
                )
                session.execute(
                    insert(job_runs).values(
                        id=run_id,
                        job_name="cartographer",
                        status="success" if complete else "partial_success",
                        started_at=started,
                        completed_at=datetime.now(UTC),
                        counts_json=json.dumps(
                            {
                                "summary": summary.model_dump(mode="json"),
                                "message": message.model_dump(mode="json"),
                                "history_run_ids": [history.run_id for history in selected],
                            },
                            sort_keys=True,
                        ),
                        safe_error_code=None,
                        safe_error_message=None,
                    )
                )
                return AnalysisResult(summary, message)
        except Exception:
            try:
                with GraphRepository(self._source).transaction() as session:
                    session.execute(
                        insert(job_runs).values(
                            id=run_id,
                            job_name="cartographer",
                            status="failed",
                            started_at=started,
                            completed_at=datetime.now(UTC),
                            counts_json="{}",
                            safe_error_code="cartographer_analysis_failed",
                            safe_error_message="Cartographer analysis failed",
                        )
                    )
            except Exception:
                pass
            raise CartographerError("cartographer_analysis_failed") from None

    async def backfill(
        self,
        mint: str,
        *,
        observed_at: datetime,
        adapter: WalletHistoryAdapter | None = None,
    ) -> AnalysisResult:
        _validate_base58_address(mint)
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise CartographerError("cartographer_time_invalid")
        if adapter is None and self._settings.ingest_mode != "live":
            raise CartographerError("cartographer_requires_explicit_live_mode")
        session, owned = RawJournal(self._source)._session()
        try:
            _, wallets, _, _ = self._scope(session, mint)
            anchors = {wallet: first_activity(session, mint, wallet) for wallet in wallets}
        finally:
            if owned:
                session.close()
        reader = adapter or WalletHistoryAdapter(self._settings, self._source)
        histories: dict[str, WalletHistoryResult] = {}
        warnings: set[str] = set()
        for wallet in wallets:
            anchor = anchors[wallet]
            end = (
                min(observed_at, anchor.block_time + timedelta(seconds=1))
                if anchor and anchor.block_time
                else observed_at
            )
            end = end.replace(microsecond=0)
            try:
                histories[wallet] = await reader.fetch(
                    HistoryQuery(
                        wallet=wallet,
                        start_time=end - timedelta(days=self._settings.cartographer_lookback_days),
                        end_time=end,
                        max_transactions=self._settings.cartographer_max_history_tx,
                        page_size=min(
                            self._settings.backfill_page_size,
                            self._settings.cartographer_max_history_tx,
                        ),
                    )
                )
            except Exception:
                warnings.add("history_fetch_failed")
        return self.analyze(
            mint, histories, observed_at=observed_at, extra_warnings=tuple(sorted(warnings))
        )

    def get_summary(
        self, *, run_id: str | None = None, mint: str | None = None
    ) -> CartographerSummary | None:
        session, owned = RawJournal(self._source)._session()
        try:
            query = select(job_runs).where(
                job_runs.c.job_name == "cartographer",
                job_runs.c.status.in_(["success", "partial_success"]),
            )
            if run_id is not None:
                query = query.where(job_runs.c.id == run_id)
            if mint is not None:
                _validate_base58_address(mint)
                query = query.where(
                    func.json_extract(job_runs.c.counts_json, "$.summary.mint") == mint
                )
            for row in session.execute(
                query.order_by(job_runs.c.completed_at.desc(), job_runs.c.id.desc()).limit(1)
            ).mappings():
                summary = CartographerSummary.model_validate(
                    json.loads(row["counts_json"])["summary"]
                )
                if mint is None or summary.mint == mint:
                    return summary
            return None
        finally:
            if owned:
                session.close()
