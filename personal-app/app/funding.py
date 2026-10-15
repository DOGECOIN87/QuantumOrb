"""Bounded funding observations from durable history and canonical token activity."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import Settings, _validate_base58_address
from app.db.graph import ALGORITHM_VERSION, GraphRepository, make_node, utc
from app.db.models import job_runs, raw_events, scout_events
from app.db.raw_journal import SessionSource
from app.domain.enums import GraphEdgeStrength, GraphEdgeType, GraphNodeType
from app.domain.models import GraphEdgeEvidence
from app.integrations.wallet_history import WalletHistoryResult
from app.transfers import TransferExtractor, TransferFact


class FundingError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("funding analysis could not be completed")


@dataclass(frozen=True)
class Activity:
    event_key: str
    signature: str
    slot: int
    transaction_index: int | None
    instruction_index: int | None
    block_time: datetime | None
    finality: str


def first_activity(session: Session, mint: str, wallet: str) -> Activity | None:
    row = session.execute(
        select(scout_events)
        .where(
            scout_events.c.mint == mint,
            or_(scout_events.c.user == wallet, scout_events.c.creator == wallet),
            scout_events.c.decode_status == "decoded",
            scout_events.c.finality_status != "reorged_or_changed",
        )
        .order_by(
            scout_events.c.slot,
            scout_events.c.transaction_index.nulls_first(),
            scout_events.c.instruction_index.nulls_first(),
            scout_events.c.id,
        )
        .limit(1)
    ).first()
    if row is None:
        return None
    data = row._mapping
    return Activity(
        data["event_key"],
        data["signature"],
        data["slot"],
        data["transaction_index"],
        data["instruction_index"],
        utc(data["block_time"]) if data["block_time"] else None,
        data["finality_status"],
    )


def verify_history(session: Session, history: WalletHistoryResult) -> None:
    """Bind coverage and transaction bodies to T021's durable audit and journal."""
    try:
        run = session.execute(select(job_runs).where(job_runs.c.id == history.run_id)).one()
        audit = json.loads(run._mapping["counts_json"])
        if (
            run._mapping["job_name"] != "wallet_history"
            or run._mapping["completed_at"] is None
            or audit["query"] != history.query.model_dump(mode="json")
            or audit["coverage"] != json.loads(json.dumps(asdict(history.coverage)))
            or audit["page_raw_event_ids"] != list(history.page_raw_event_ids)
            or len(history.transactions) != history.coverage.transactions_returned
            or len({item.signature for item in history.transactions}) != len(history.transactions)
        ):
            raise ValueError
        for item in history.transactions:
            raw = session.execute(
                select(raw_events).where(raw_events.c.id == item.raw_event_id)
            ).one()
            if (
                item.raw_event_id not in audit["transaction_raw_event_ids"]
                or item.page_raw_event_id not in history.page_raw_event_ids
                or raw._mapping["payload_json"] != item.payload_json
                or raw._mapping["signature"] != item.signature
                or raw._mapping["slot"] != item.slot
            ):
                raise ValueError
    except Exception:
        raise FundingError("funding_history_provenance_invalid") from None


@dataclass(frozen=True)
class FundingLink:
    source_wallet: str
    target_wallet: str
    fact: TransferFact
    edge_id: int
    time_delta_seconds: int | None


@dataclass(frozen=True)
class FundingResult:
    mint: str
    wallet: str
    links: tuple[FundingLink, ...]
    history_complete: bool
    warnings: tuple[str, ...]
    transactions_scanned: int
    transfers_seen: int
    nodes_added: int
    edges_added: int


def _before(fact: TransferFact, activity: Activity) -> bool | None:
    if fact.slot != activity.slot:
        return fact.slot < activity.slot
    if activity.transaction_index is None:
        return None
    if fact.transaction_index != activity.transaction_index:
        return fact.transaction_index < activity.transaction_index
    if fact.signature != activity.signature or activity.instruction_index is None:
        return None
    return fact.top_level_instruction_index < activity.instruction_index


class FundingDetector:
    def __init__(self, source: SessionSource, settings: Settings) -> None:
        self._repo = GraphRepository(source)
        self._settings = Settings.model_validate(settings.model_dump())

    def analyze(
        self,
        mint: str,
        wallet: str,
        history: WalletHistoryResult,
        *,
        observed_at: datetime,
    ) -> FundingResult:
        _validate_base58_address(mint)
        _validate_base58_address(wallet)
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise FundingError("funding_time_invalid")
        if history.query.wallet != wallet:
            raise FundingError("funding_wallet_mismatch")
        if self._settings.solana_network != "mainnet-beta":
            raise FundingError("funding_network_mismatch")
        if (
            len(history.transactions) > self._settings.cartographer_max_history_tx
            or history.query.end_time - history.query.start_time
            > timedelta(days=self._settings.cartographer_lookback_days)
        ):
            raise FundingError("funding_history_limit")
        with self._repo.transaction() as session:
            verify_history(session, history)
            activity = first_activity(session, mint, wallet)
            if activity is None:
                raise FundingError("funding_activity_unavailable")
            warnings = set(history.coverage.warnings) | set(history.coverage.incomplete_reasons)
            if not history.coverage.history_complete:
                warnings.add("history_incomplete")
            if activity.finality == "provisional":
                warnings.add("provisional_token_activity")
            candidates: dict[str, tuple[str, TransferFact, int | None]] = {}
            transfers_seen = 0
            for transaction in history.transactions:
                result = TransferExtractor().extract(transaction)
                warnings.update(result.warnings)
                if not result.complete:
                    warnings.add("transfer_extraction_incomplete")
                transfers_seen += len(result.transfers)
                for fact in result.transfers:
                    if fact.context != "transfer":
                        continue
                    sender = fact.source_account if fact.asset == "sol" else fact.source_owner
                    recipient = (
                        fact.destination_account if fact.asset == "sol" else fact.destination_owner
                    )
                    if recipient != wallet:
                        continue
                    if sender is None:
                        warnings.add("funding_owner_unavailable")
                        continue
                    if sender == wallet:
                        continue
                    if fact.net_amount_raw is None:
                        warnings.add("funding_net_amount_unavailable")
                        continue
                    if int(fact.net_amount_raw) == 0:
                        continue
                    before = _before(fact, activity)
                    if before is None:
                        warnings.add("funding_order_unavailable")
                        continue
                    if not before:
                        continue
                    delta: int | None = None
                    if activity.block_time is not None and fact.block_time is not None:
                        delta = int(activity.block_time.timestamp()) - fact.block_time
                        if delta < 0:
                            warnings.add("funding_time_conflict")
                            continue
                    else:
                        warnings.add("funding_time_unavailable")
                    candidates[fact.fact_key] = (sender, fact, delta)
            minimum = self._settings.cartographer_min_funding_lamports
            preferred = {
                key: item
                for key, item in candidates.items()
                if item[1].asset != "sol" or int(item[1].amount_raw) >= minimum
            }
            if candidates and len(preferred) != len(candidates):
                if preferred:
                    candidates = preferred
                    warnings.add("small_funding_transfers_filtered")
                else:
                    warnings.add("below_minimum_funding_only_evidence")
            counterparties: set[str] = set()
            links: list[FundingLink] = []
            nodes_added = edges_added = 0
            repo = GraphRepository(session)
            for sender, fact, delta in sorted(
                candidates.values(),
                key=lambda item: (
                    item[1].slot,
                    item[1].transaction_index,
                    item[1].top_level_instruction_index,
                    item[1].inner_instruction_index or -1,
                    item[1].fact_key,
                ),
            ):
                if sender not in counterparties and len(counterparties) >= (
                    self._settings.cartographer_max_counterparties_per_wallet
                ):
                    warnings.add("counterparty_limit_reached")
                    continue
                counterparties.add(sender)
                details: dict[str, Any] = {
                    "mint": mint,
                    "fact_key": fact.fact_key,
                    "asset": fact.asset,
                    "amount_raw": fact.amount_raw,
                    "net_amount_raw": fact.net_amount_raw,
                    "fee_amount_raw": fact.fee_amount_raw,
                    "transfer_mint": fact.mint,
                    "source_account": fact.source_account,
                    "destination_account": fact.destination_account,
                    "time_delta_seconds": delta,
                    "funding_block_time": fact.block_time,
                    "activity_event_key": activity.event_key,
                    "activity_finality": activity.finality,
                    "instruction_index": fact.top_level_instruction_index,
                    "inner_instruction_index": fact.inner_instruction_index,
                    "raw_event_ids": [fact.raw_event_id],
                    "page_raw_event_id": fact.page_raw_event_id,
                    "history_complete": history.coverage.history_complete,
                    "history_query": history.query.model_dump(mode="json"),
                    "label": "funding_link_observed",
                }
                write = repo.record_relationship(
                    make_node(GraphNodeType.WALLET, sender, observed_at),
                    make_node(GraphNodeType.WALLET, wallet, observed_at),
                    GraphEdgeEvidence(
                        edge_type=GraphEdgeType.FUNDED_WALLET,
                        strength=GraphEdgeStrength.DIRECT,
                        evidence_type="shared_funder",
                        source="helius_getTransactionsForAddress",
                        signature=fact.signature,
                        slot=fact.slot,
                        details=details,
                        observed_at=observed_at,
                        algorithm_version=ALGORITHM_VERSION,
                    ),
                )
                nodes_added += write.nodes_added
                edges_added += write.edges_added
                assert write.edge.id is not None
                links.append(FundingLink(sender, wallet, fact, write.edge.id, delta))
            return FundingResult(
                mint,
                wallet,
                tuple(links),
                history.coverage.history_complete and not warnings,
                tuple(sorted(warnings)),
                len(history.transactions),
                transfers_seen,
                nodes_added,
                edges_added,
            )
