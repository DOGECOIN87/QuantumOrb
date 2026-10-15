"""Bounded, read-only dashboard projections. Never initiates provider work."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, Field
from sqlalchemy import Select, case, func, or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.graph import utc
from app.db.models import (
    graph_edges,
    graph_nodes,
    job_runs,
    raw_events,
    scout_events,
    token_lifecycles,
    tokens,
)
from app.db.raw_journal import RawJournal, SessionSource
from app.operations import Operations

Window = Literal["1h", "24h", "7d", "all"]
TRADES = ("curve_buy", "curve_sell", "pool_buy", "pool_sell")
LIFECYCLES = {
    "seen_create",
    "curve_active",
    "curve_trading",
    "curve_complete",
    "migration_seen",
    "pool_active",
    "pool_trading",
    "error",
}


class ActivityItem(BaseModel):
    event_key: str
    event_type: str
    signature: str
    slot: int
    mint: str | None
    wallet: str | None
    creator: str | None
    observed_at: datetime
    block_time: datetime | None
    finality: str
    program: str


class TokenItem(BaseModel):
    mint: str
    creator: str | None
    lifecycle: str
    first_seen_at: datetime
    last_seen_at: datetime
    provisional: bool | None
    data_gap: bool | None
    pool: str | None
    event_count: int


class WalletItem(BaseModel):
    wallet: str
    event_count: int
    trade_count: int
    token_count: int
    finalized_count: int
    last_seen_at: datetime


T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int
    has_more: bool
    scope: str = "local_index"


class DashboardSnapshot(BaseModel):
    observed_at: datetime
    source: str = "local"
    scope: str = "local_index"
    time_basis: str = "local_observation_time"
    health: dict[str, Any]
    counters: dict[str, int | None]
    series: list[dict[str, str | int]]
    recent: list[ActivityItem]
    agents: list[dict[str, Any]]
    latest_slot: int | None
    last_observation_at: datetime | None
    warnings: list[str] = Field(default_factory=list)


def _cutoff(window: Window, now: datetime) -> datetime | None:
    return (
        None
        if window == "all"
        else now
        - {
            "1h": timedelta(hours=1),
            "24h": timedelta(hours=24),
            "7d": timedelta(days=7),
        }[window]
    )


def _bounds(limit: int, offset: int, q: str) -> None:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("invalid dashboard page limit")
    if type(offset) is not int or not 0 <= offset <= 100_000:
        raise ValueError("invalid dashboard page offset")
    if q and (len(q) > 88 or re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]+", q) is None):
        raise ValueError("search must be a public address or signature prefix")


def _event(row: Any) -> ActivityItem:
    return ActivityItem(
        event_key=row["event_key"],
        event_type=row["event_type"],
        signature=row["signature"],
        slot=row["slot"],
        mint=row["mint"],
        wallet=row["user"],
        creator=row["creator"],
        observed_at=utc(row["observed_at"]),
        block_time=utc(row["block_time"]) if row["block_time"] else None,
        finality=row["finality_status"],
        program=row["program"],
    )


def _events_query(q: str, window: Window, now: datetime) -> Select[Any]:
    query = select(scout_events)
    cutoff = _cutoff(window, now)
    if cutoff:
        query = query.where(scout_events.c.observed_at >= cutoff)
    if q:
        query = query.where(
            or_(
                *(
                    column.startswith(q, autoescape=True)
                    for column in (
                        scout_events.c.mint,
                        scout_events.c.user,
                        scout_events.c.creator,
                        scout_events.c.signature,
                    )
                )
            )
        )
    return query


class DashboardRepository:
    def __init__(self, source: SessionSource, settings: Settings) -> None:
        self.source = source
        self.settings = settings

    def activity(
        self,
        *,
        q: str = "",
        window: Window = "all",
        limit: int = 20,
        offset: int = 0,
        finality: str | None = None,
        now: datetime | None = None,
    ) -> Page[ActivityItem]:
        _bounds(limit, offset, q)
        query = _events_query(q, window, now or datetime.now(UTC))
        if finality:
            if finality not in {"provisional", "finalized", "reorged_or_changed"}:
                raise ValueError("invalid finality filter")
            query = query.where(scout_events.c.finality_status == finality)
        session, owned = RawJournal(self.source)._session()
        try:
            total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
            rows = session.execute(
                query.order_by(scout_events.c.observed_at.desc(), scout_events.c.id.desc())
                .limit(limit)
                .offset(offset)
            ).mappings()
            items = [_event(row) for row in rows]
            return Page(
                items=items,
                total=total,
                limit=limit,
                offset=offset,
                has_more=offset + len(items) < total,
            )
        finally:
            if owned:
                session.close()

    def token_list(
        self,
        *,
        q: str = "",
        lifecycle: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Page[TokenItem]:
        _bounds(limit, offset, q)
        if lifecycle and lifecycle not in LIFECYCLES:
            raise ValueError("invalid lifecycle filter")
        counts = (
            select(scout_events.c.mint, func.count().label("event_count"))
            .where(scout_events.c.finality_status != "reorged_or_changed")
            .group_by(scout_events.c.mint)
            .subquery()
        )
        query = (
            select(
                tokens.c.mint,
                func.coalesce(token_lifecycles.c.creator, tokens.c.creator_wallet).label(
                    "creator_wallet"
                ),
                tokens.c.first_seen_at,
                tokens.c.last_seen_at,
                token_lifecycles.c.state,
                token_lifecycles.c.provisional,
                token_lifecycles.c.data_gap,
                token_lifecycles.c.pool,
                func.coalesce(counts.c.event_count, 0).label("event_count"),
            )
            .outerjoin(token_lifecycles, tokens.c.mint == token_lifecycles.c.mint)
            .outerjoin(counts, counts.c.mint == tokens.c.mint)
        )
        if q:
            query = query.where(
                or_(
                    tokens.c.mint.startswith(q, autoescape=True),
                    func.coalesce(token_lifecycles.c.creator, tokens.c.creator_wallet).startswith(
                        q, autoescape=True
                    ),
                )
            )
        if lifecycle:
            query = query.where(token_lifecycles.c.state == lifecycle)
        session, owned = RawJournal(self.source)._session()
        try:
            total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
            rows = session.execute(
                query.order_by(tokens.c.last_seen_at.desc(), tokens.c.mint)
                .limit(limit)
                .offset(offset)
            ).mappings()
            items = [
                TokenItem(
                    mint=row["mint"],
                    creator=row["creator_wallet"],
                    lifecycle=row["state"] or "not_read",
                    first_seen_at=utc(row["first_seen_at"]),
                    last_seen_at=utc(row["last_seen_at"]),
                    provisional=row["provisional"],
                    data_gap=row["data_gap"],
                    pool=row["pool"],
                    event_count=row["event_count"],
                )
                for row in rows
            ]
            return Page(
                items=items,
                total=total,
                limit=limit,
                offset=offset,
                has_more=offset + len(items) < total,
            )
        finally:
            if owned:
                session.close()

    def wallets(
        self,
        *,
        q: str = "",
        window: Window = "all",
        limit: int = 20,
        offset: int = 0,
        now: datetime | None = None,
    ) -> Page[WalletItem]:
        _bounds(limit, offset, q)
        events = (
            _events_query("", window, now or datetime.now(UTC))
            .where(
                scout_events.c.user.is_not(None),
                scout_events.c.finality_status != "reorged_or_changed",
            )
            .subquery()
        )
        query = select(
            events.c.user.label("wallet"),
            func.count().label("event_count"),
            func.sum(case((events.c.event_type.in_(TRADES), 1), else_=0)).label("trade_count"),
            func.count(func.distinct(events.c.mint)).label("token_count"),
            func.sum(case((events.c.finality_status == "finalized", 1), else_=0)).label(
                "finalized_count"
            ),
            func.max(events.c.observed_at).label("last_seen_at"),
        ).group_by(events.c.user)
        if q:
            query = query.where(events.c.user.startswith(q, autoescape=True))
        session, owned = RawJournal(self.source)._session()
        try:
            total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
            rows = session.execute(
                query.order_by(func.max(events.c.observed_at).desc(), events.c.user)
                .limit(limit)
                .offset(offset)
            ).mappings()
            items = [
                WalletItem(**{**dict(row), "last_seen_at": utc(row["last_seen_at"])})
                for row in rows
            ]
            return Page(
                items=items,
                total=total,
                limit=limit,
                offset=offset,
                has_more=offset + len(items) < total,
            )
        finally:
            if owned:
                session.close()

    def snapshot(self, *, now: datetime | None = None) -> DashboardSnapshot:
        observed = utc(now or datetime.now(UTC))
        health = Operations(self.source, self.settings).health()
        counters: dict[str, int | None] = dict.fromkeys(
            (
                "launches_24h",
                "pools_24h",
                "wallets_1h",
                "relationships",
                "raw_observations",
                "analyses",
                "events",
                "tokens",
                "finalized",
                "provisional",
                "nodes",
            )
        )
        recent: list[ActivityItem] = []
        series: list[dict[str, str | int]] = []
        latest_slot = None
        last_observation = None
        warnings: list[str] = []
        if health["ready"]:
            session, owned = RawJournal(self.source)._session()
            try:
                self._counters(session, observed, counters, self.settings.solana_network)
                latest_slot = session.scalar(select(func.max(scout_events.c.slot)))
                last_observation = session.scalar(select(func.max(scout_events.c.observed_at)))
                recent = [
                    _event(row)
                    for row in session.execute(
                        select(scout_events)
                        .order_by(scout_events.c.observed_at.desc(), scout_events.c.id.desc())
                        .limit(12)
                    ).mappings()
                ]
                # SQL aggregates into 24 fixed UTC hours; no unbounded history materialization.
                start = observed.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)
                bucket = func.strftime("%Y-%m-%dT%H:00:00Z", scout_events.c.observed_at)
                grouped = {
                    row["hour"]: row["count"]
                    for row in session.execute(
                        select(bucket.label("hour"), func.count().label("count"))
                        .where(scout_events.c.observed_at >= start)
                        .group_by(bucket)
                    ).mappings()
                }
                series = [
                    {
                        "hour": (start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00:00Z"),
                        "events": grouped.get(
                            (start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00:00Z"), 0
                        ),
                    }
                    for i in range(24)
                ]
            finally:
                if owned:
                    session.close()
        else:
            warnings.append("database_unavailable")
        if last_observation and observed - utc(last_observation) > timedelta(minutes=5):
            warnings.append("observations_stale")
        if not recent:
            warnings.append("canonical_events_not_read")
        warnings.append("provider_connection_not_verified")
        return DashboardSnapshot(
            observed_at=observed,
            health=health,
            counters=counters,
            series=series,
            recent=recent,
            agents=self._agents(counters),
            latest_slot=latest_slot,
            last_observation_at=utc(last_observation) if last_observation else None,
            warnings=warnings,
        )

    @staticmethod
    def _counters(
        session: Session, now: datetime, counters: dict[str, int | None], network: str
    ) -> None:
        valid = scout_events.c.finality_status != "reorged_or_changed"
        day = scout_events.c.observed_at >= now - timedelta(days=1)
        counters.update(
            launches_24h=session.scalar(
                select(func.count(func.distinct(scout_events.c.mint))).where(
                    valid, day, scout_events.c.event_type == "token_created"
                )
            )
            or 0,
            pools_24h=session.scalar(
                select(func.count(func.distinct(scout_events.c.pool))).where(
                    valid, day, scout_events.c.event_type == "pool_created"
                )
            )
            or 0,
            wallets_1h=session.scalar(
                select(func.count(func.distinct(scout_events.c.user))).where(
                    valid, scout_events.c.observed_at >= now - timedelta(hours=1)
                )
            )
            or 0,
        )
        for key, table in (
            ("tokens", tokens),
            ("events", scout_events),
        ):
            counters[key] = session.scalar(select(func.count()).select_from(table)) or 0
        counters["nodes"] = (
            session.scalar(
                select(func.count())
                .select_from(graph_nodes)
                .where(graph_nodes.c.network == network)
            )
            or 0
        )
        counters["relationships"] = (
            session.scalar(
                select(func.count())
                .select_from(graph_edges)
                .join(graph_nodes, graph_edges.c.source_node_id == graph_nodes.c.id)
                .where(graph_nodes.c.network == network)
            )
            or 0
        )
        for status in ("finalized", "provisional"):
            counters[status] = (
                session.scalar(
                    select(func.count())
                    .select_from(scout_events)
                    .where(scout_events.c.finality_status == status)
                )
                or 0
            )
        counters["analyses"] = (
            session.scalar(
                select(func.count())
                .select_from(job_runs)
                .where(
                    job_runs.c.job_name == "cartographer",
                    job_runs.c.status.in_(["success", "partial_success"]),
                )
            )
            or 0
        )
        counters["raw_observations"] = (
            session.scalar(select(func.count()).select_from(raw_events)) or 0
        )

    @staticmethod
    def _agents(counters: dict[str, int | None]) -> list[dict[str, Any]]:
        return [
            {
                "id": "scout",
                "code": "SCT",
                "name": "Scout",
                "color": "blue",
                "state": "observed" if counters["events"] else "not_read",
                "metric": counters["events"],
                "unit": "canonical events",
                "description": "Journals observations and decodes launch, trade and pool events.",
                "input": "Local raw journal",
                "output": "Canonical events → Cartographer",
            },
            {
                "id": "cartographer",
                "code": "MAP",
                "name": "Cartographer",
                "color": "purple",
                "state": "observed" if counters["analyses"] else "not_read",
                "metric": counters["relationships"],
                "unit": "stored relationships",
                "description": (
                    "Maps observed funding and transaction relationships with bounded coverage."
                ),
                "input": "Canonical events + recorded wallet histories",
                "output": "Evidence graph",
            },
            *[
                {
                    "id": name.lower(),
                    "code": code,
                    "name": name,
                    "color": color,
                    "state": "not_implemented",
                    "metric": None,
                    "unit": "not available",
                    "description": description,
                    "input": "Not connected",
                    "output": "Not available",
                }
                for name, code, color, description in (
                    (
                        "Judge",
                        "JDG",
                        "green",
                        "Probability judgement is planned; no model is running.",
                    ),
                    (
                        "Quant",
                        "QNT",
                        "orange",
                        "Pricing and sizing research is planned; no estimates are emitted.",
                    ),
                    (
                        "Guard",
                        "GRD",
                        "red",
                        "Risk screening is planned; missing evidence is never a pass.",
                    ),
                    (
                        "Executor",
                        "EXE",
                        "yellow",
                        "Execution is disabled. This application is read-only.",
                    ),
                )
            ],
        ]
