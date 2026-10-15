"""Deterministic Scout lifecycle projections over retained canonical events."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Literal, cast

from sqlalchemy import func, insert, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.db.canonical_events import ScoutEventRecord, _scout_record
from app.db.models import reconciliation_records, scout_events, token_lifecycles, tokens
from app.normalization import _validate_public_key

SessionSource = Session | sessionmaker[Session] | Callable[[], Session]
LifecycleState = Literal[
    "seen_create",
    "curve_active",
    "curve_trading",
    "curve_complete",
    "migration_seen",
    "pool_active",
    "pool_trading",
    "error",
]
_STATES: dict[str, LifecycleState] = {
    "token_created": "seen_create",
    "curve_buy": "curve_trading",
    "curve_sell": "curve_trading",
    "curve_complete": "curve_complete",
    "migration": "migration_seen",
    "pool_created": "pool_active",
    "pool_buy": "pool_trading",
    "pool_sell": "pool_trading",
}
_RANK = {
    state: rank
    for rank, state in enumerate(
        (
            "seen_create",
            "curve_active",
            "curve_trading",
            "curve_complete",
            "migration_seen",
            "pool_active",
            "pool_trading",
        )
    )
}


class LifecycleError(ValueError):
    """A stored event cannot safely contribute to a lifecycle."""


class LifecycleContextUnavailable(LifecycleError):
    """No unique observed mint is available; retain the event for later replay."""


@dataclass(frozen=True)
class TokenLifecycleRecord:
    """The current token lifecycle, including explicit observation uncertainty."""

    mint: str
    state: LifecycleState
    first_seen_slot: int
    last_seen_slot: int
    bonding_curve: str | None
    pool: str | None
    creator: str | None
    user: str | None
    quote_mint: str | None
    provisional: bool
    data_gap: bool
    last_event_id: int
    updated_at: datetime


def _order(event: ScoutEventRecord) -> tuple[int, int, int, int, str, str]:
    """Unknown indices sort before known ones; identity breaks ties reproducibly."""
    return (
        event.slot,
        event.transaction_index if event.transaction_index is not None else -1,
        event.instruction_index if event.instruction_index is not None else -1,
        event.event_index,
        event.signature,
        event.event_key,
    )


def _ambiguous_order(events: Sequence[ScoutEventRecord]) -> bool:
    by_slot: dict[int, list[ScoutEventRecord]] = {}
    for event in events:
        by_slot.setdefault(event.slot, []).append(event)
    for slot_events in by_slot.values():
        by_signature: dict[str, list[ScoutEventRecord]] = {}
        for event in slot_events:
            by_signature.setdefault(event.signature, []).append(event)
        for transaction_events in by_signature.values():
            if len({event.transaction_index for event in transaction_events}) > 1:
                return True
            if len(transaction_events) > 1:
                if any(event.instruction_index is None for event in transaction_events):
                    return True
                if len(
                    {(event.instruction_index, event.event_index) for event in transaction_events}
                ) != len(transaction_events):
                    return True
        signatures = {event.signature for event in slot_events}
        if len(signatures) > 1:
            # Partial or contradictory transaction positions cannot establish chain order.
            positions: dict[str, set[int | None]] = {}
            for event in slot_events:
                positions.setdefault(event.signature, set()).add(event.transaction_index)
            if any(None in indices or len(indices) != 1 for indices in positions.values()):
                return True
            if len({next(iter(indices)) for indices in positions.values()}) != len(signatures):
                return True
    return False


def project_lifecycle(
    mint: str, events: Sequence[ScoutEventRecord], *, data_gap: bool = False
) -> TokenLifecycleRecord:
    """Replay observations in protocol order without reversing an observed phase."""
    try:
        _validate_public_key(mint)
    except ValueError:
        raise LifecycleError("lifecycle mint is invalid") from None
    if not events:
        raise LifecycleError("lifecycle requires stored decoded events")
    for event in events:
        if event.decode_status != "decoded" or event.event_type not in _STATES:
            raise LifecycleError("lifecycle requires supported decoded events")
        if event.mint is not None and event.mint != mint:
            raise LifecycleError("event mint does not match lifecycle mint")
        if event.observed_at.tzinfo is None or event.observed_at.utcoffset() is None:
            raise LifecycleError("event observation time must be timezone-aware")
        if any(
            value is not None and (type(value) is not int or value < 0)
            for value in (
                event.slot,
                event.transaction_index,
                event.instruction_index,
                event.event_index,
            )
        ):
            raise LifecycleError("event ordering metadata is invalid")
    ordered = sorted(events, key=_order)
    state = _STATES[ordered[0].event_type]
    gap = data_gap or _ambiguous_order(ordered)
    seen: set[str] = set()
    bonding_curve = pool = creator = user = quote_mint = None
    creation_seen = False
    pump_observed = any(event.program == "pump" for event in ordered)
    provisional = False
    for event in ordered:
        target = _STATES[event.event_type]
        if event.event_name == "PostCompleteBuyEvent":
            target = "curve_complete"
        if event.event_type != "token_created" and "token_created" not in seen:
            gap = True
        if event.event_type == "migration" and "curve_complete" not in seen:
            gap = True
        if event.event_type == "pool_created" and pump_observed and "migration" not in seen:
            gap = True
        if event.event_type in {"pool_buy", "pool_sell"} and "pool_created" not in seen:
            gap = True
        if _RANK[target] >= _RANK[state]:
            state = target
        else:
            gap = True
        seen.add(event.event_type)
        if target == "curve_complete":
            seen.add("curve_complete")
        uncertain = (
            event.finality_status == "reorged_or_changed"
            or event.reconciliation_status in {"changed", "missing"}
        )
        provisional |= event.finality_status != "finalized" or uncertain
        gap |= uncertain
        if event.event_type == "token_created":
            # Launch identities must not be replaced by subsequent trade participants.
            creator, user = event.creator, event.user
            creation_seen = True
        elif not creation_seen and event.event_type == "pool_created":
            creator = event.creator
        for name, value in (
            ("bonding_curve", event.bonding_curve),
            ("pool", event.pool),
            ("quote_mint", event.quote_mint),
        ):
            if value is not None:
                if name == "bonding_curve":
                    bonding_curve = value
                elif name == "pool":
                    pool = value
                else:
                    quote_mint = value
    return TokenLifecycleRecord(
        mint=mint,
        state=state,
        first_seen_slot=ordered[0].slot,
        last_seen_slot=ordered[-1].slot,
        bonding_curve=bonding_curve,
        pool=pool,
        creator=creator,
        user=user,
        quote_mint=quote_mint,
        provisional=provisional,
        data_gap=gap,
        last_event_id=ordered[-1].id,
        updated_at=max(event.observed_at for event in ordered).astimezone(UTC),
    )


class LifecycleRepository:
    """Apply a stored Scout event, atomically rebuilding its token projection.

    Caller-supplied sessions retain transaction ownership. Factory-created
    sessions commit or roll back the entire projection and token placeholder.
    """

    def __init__(self, session_source: SessionSource) -> None:
        self._session_source = session_source

    @staticmethod
    def _pool_mints(session: Session, pool: str) -> set[str]:
        return set(
            session.scalars(
                select(scout_events.c.mint)
                .distinct()
                .where(
                    scout_events.c.pool == pool,
                    scout_events.c.mint.is_not(None),
                    scout_events.c.decode_status == "decoded",
                    scout_events.c.finality_status != "reorged_or_changed",
                    scout_events.c.reconciliation_status.not_in(["changed", "missing"]),
                    scout_events.c.event_type.in_(["migration", "pool_created"]),
                )
            )
        )

    def _history(self, session: Session, mint: str) -> tuple[list[ScoutEventRecord], bool]:
        pools: set[str] = set(
            session.scalars(
                select(scout_events.c.pool)
                .distinct()
                .where(
                    scout_events.c.mint == mint,
                    scout_events.c.pool.is_not(None),
                    scout_events.c.decode_status == "decoded",
                    scout_events.c.event_type.in_(["migration", "pool_created"]),
                )
            )
        )
        unique_pools = {pool for pool in pools if self._pool_mints(session, pool) == {mint}}
        rows = session.execute(
            select(scout_events).where(
                or_(
                    scout_events.c.mint == mint,
                    (scout_events.c.mint.is_(None))
                    & scout_events.c.pool.in_(unique_pools)
                    & scout_events.c.event_type.in_(["pool_buy", "pool_sell"]),
                )
            )
        )
        history = [_scout_record(row) for row in rows]
        provisional_keys = [
            event.event_key
            for event in history
            if event.finality_status == "provisional" and event.reconciliation_status == "pending"
        ]
        failed = False
        if provisional_keys:
            latest_ids = (
                select(func.max(reconciliation_records.c.id))
                .where(reconciliation_records.c.event_key.in_(provisional_keys))
                .group_by(reconciliation_records.c.event_key)
            )
            failed = (
                session.scalar(
                    select(reconciliation_records.c.id)
                    .where(
                        reconciliation_records.c.id.in_(latest_ids),
                        reconciliation_records.c.status == "failed",
                    )
                    .limit(1)
                )
                is not None
            )
        return history, pools != unique_pools or failed

    def apply(self, event: ScoutEventRecord) -> TokenLifecycleRecord:
        """Reload authoritative finality; never trust a stale caller snapshot."""
        if isinstance(self._session_source, Session):
            session, owned = self._session_source, False
        else:
            session, owned = self._session_source(), True
        try:
            if not owned:
                self._begin_sqlite_transaction(session)
            with session.begin() if owned else session.begin_nested():
                if owned:
                    self._begin_sqlite_transaction(session)
                row = session.execute(
                    select(scout_events).where(scout_events.c.id == event.id)
                ).first()
                if row is None or row._mapping["event_key"] != event.event_key:
                    raise LifecycleError("lifecycle event must be persisted first")
                stored = _scout_record(row)
                if stored.decode_status != "decoded" or stored.event_type not in _STATES:
                    raise LifecycleError("lifecycle requires supported decoded events")
                mint = stored.mint
                if mint is None:
                    mints = self._pool_mints(session, stored.pool) if stored.pool else set()
                    if stored.event_type not in {"pool_buy", "pool_sell"} or len(mints) != 1:
                        raise LifecycleContextUnavailable("event has no unique observed mint")
                    mint = next(iter(mints))
                history, gap = self._history(session, mint)
                projection = project_lifecycle(mint, history, data_gap=gap)
                existing = session.execute(
                    select(token_lifecycles).where(token_lifecycles.c.mint == mint)
                ).first()
                if existing is not None:
                    previous = existing._mapping
                    previous_state = previous["state"]
                    if previous_state not in _RANK and previous_state != "error":
                        raise LifecycleError("stored lifecycle phase is unsupported")
                    if previous_state == "error" or _RANK[previous_state] > _RANK[projection.state]:
                        # A pool association can become disputed after its
                        # trades were applied. Keep the observed phase with
                        # uncertainty rather than silently reversing it.
                        projection = replace(
                            projection,
                            state=cast(LifecycleState, previous_state),
                            provisional=True,
                            data_gap=True,
                        )
                self._ensure_token(session, mint, history)
                values = asdict(projection)
                if existing is None:
                    session.execute(insert(token_lifecycles).values(values))
                else:
                    session.execute(
                        update(token_lifecycles)
                        .where(token_lifecycles.c.mint == mint)
                        .values(values)
                    )
                return projection
        finally:
            if owned:
                session.close()

    @staticmethod
    def _begin_sqlite_transaction(session: Session) -> None:
        # sqlite3's legacy transaction mode does not begin a transaction for
        # SELECT or SAVEPOINT. Start the physical transaction before reading
        # history, so savepoint release cannot commit a caller's transaction
        # and concurrent writers cannot publish an older projection.
        connection = session.connection()
        if connection.dialect.name == "sqlite":
            dbapi = connection.connection.driver_connection
            if not getattr(dbapi, "in_transaction", False):
                connection.exec_driver_sql("BEGIN IMMEDIATE")

    @staticmethod
    def _ensure_token(session: Session, mint: str, events: Sequence[ScoutEventRecord]) -> None:
        if session.execute(select(tokens.c.mint).where(tokens.c.mint == mint)).first() is not None:
            return
        first = min(event.observed_at for event in events)
        last = max(event.observed_at for event in events)
        session.execute(
            insert(tokens).values(
                mint=mint,
                first_seen_at=first,
                last_seen_at=last,
                metadata_status="unknown",
                created_at=first,
                updated_at=last,
            )
        )
