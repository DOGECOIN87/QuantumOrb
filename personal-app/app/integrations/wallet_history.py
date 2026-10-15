"""Bounded, journal-first wallet history with explicit query-scoped coverage."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import insert, update

from app.config import Settings
from app.db.decode_outcomes import DecodeOutcomeConflictError, DecodeOutcomeRepository
from app.db.models import job_runs
from app.db.raw_journal import RawEventRecord, RawJournal, SessionSource
from app.db.rpc_journal import RpcResponseJournal
from app.domain.enums import DecodeStatus, EvidenceState, Transport
from app.domain.models import _signature
from app.integrations.helius_http import HeliusHttpClient
from app.integrations.logs_subscribe import RpcReader
from app.integrations.wallet_history_models import (
    HistoryQuery,
    HistoryTransaction,
    HistoryValidationError,
    parse_history_page,
    parse_history_transaction,
)
from app.logging_config import SafeError

_TOKEN_ACCOUNT_START = datetime(2022, 12, 1, tzinfo=UTC)
_TOKEN_ACCOUNT_SLOT = 111_491_819


class WalletHistoryError(SafeError):
    def __init__(self, code: str) -> None:
        super().__init__(code, "Wallet history operation failed")


@dataclass(frozen=True)
class HistoryCoverage:
    """Complete means exhausted within this exact window and filter scope."""

    history_complete: bool
    exhausted: bool
    transactions_seen: int
    transactions_returned: int
    pages_fetched: int
    next_pagination_token: str | None
    incomplete_reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    state: EvidenceState
    safe_error_code: str | None = None


@dataclass(frozen=True)
class WalletHistoryResult:
    run_id: str
    query: HistoryQuery
    transactions: tuple[HistoryTransaction, ...]
    coverage: HistoryCoverage
    page_raw_event_ids: tuple[int, ...]


class WalletHistoryAdapter:
    """Fetch full JSON transactions without deriving transfers or graph edges."""

    def __init__(
        self,
        settings: Settings,
        session_factory: SessionSource,
        *,
        rpc: RpcReader | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        rpc_timeout: float = 45,
        max_pages: int = 100,
    ) -> None:
        if (
            type(rpc_timeout) not in {int, float}
            or not math.isfinite(rpc_timeout)
            or rpc_timeout <= 0
        ):
            raise WalletHistoryError("invalid_wallet_history_timeout")
        if type(max_pages) is not int or not 1 <= max_pages <= 100:
            raise WalletHistoryError("invalid_wallet_history_page_limit")
        self._settings = settings
        self._source = session_factory
        self._rpc = rpc
        self._clock = clock
        self._timeout = rpc_timeout
        self._max_pages = max_pages
        self._outcomes = DecodeOutcomeRepository(session_factory)
        self._running = False

    def _now(self) -> datetime:
        try:
            value = self._clock()
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise ValueError
            return value.astimezone(UTC)
        except Exception:
            raise WalletHistoryError("invalid_wallet_history_clock") from None

    def _validate_request(self, query: HistoryQuery) -> tuple[HistoryQuery, Settings]:
        try:
            settings = Settings.model_validate(self._settings.model_dump(warnings=False))
            query = HistoryQuery.model_validate(query.model_dump(warnings=False))
        except Exception:
            raise WalletHistoryError("invalid_wallet_history_query") from None
        if (
            settings.ingest_mode != "live"
            or settings.solana_network != "mainnet-beta"
            or settings.helius_api_key is None
        ):
            raise WalletHistoryError("wallet_history_requires_explicit_live_mode")
        if (
            query.max_transactions > settings.cartographer_max_history_tx
            or query.page_size > settings.backfill_page_size
            or query.end_time - query.start_time
            > timedelta(days=settings.cartographer_lookback_days)
        ):
            raise WalletHistoryError("wallet_history_configured_limit")
        if query.end_time > self._now():
            raise WalletHistoryError("wallet_history_window_in_future")
        return query, settings

    def _audit(
        self,
        run_id: str,
        query: HistoryQuery,
        started: datetime,
        *,
        coverage: HistoryCoverage | None,
        page_ids: list[int],
        transaction_ids: list[int],
        failed: bool = False,
    ) -> None:
        journal = RawJournal(self._source)
        session, owned = journal._session()
        try:
            counts = {
                "provider": "helius_getTransactionsForAddress",
                "query": query.model_dump(mode="json"),
                "max_pages": self._max_pages,
                "rpc_timeout_seconds": self._timeout,
                "coverage": asdict(coverage) if coverage else None,
                "page_raw_event_ids": page_ids,
                "transaction_raw_event_ids": transaction_ids,
            }
            values: dict[str, Any] = {
                "counts_json": json.dumps(counts, sort_keys=True, separators=(",", ":")),
                "safe_error_code": coverage.safe_error_code if coverage else None,
                "safe_error_message": "wallet history coverage is incomplete"
                if coverage and coverage.state == EvidenceState.ERROR
                else None,
            }
            if coverage is None:
                session.execute(
                    insert(job_runs).values(
                        id=run_id,
                        job_name="wallet_history",
                        status="running",
                        started_at=started,
                        completed_at=None,
                        **values,
                    )
                )
            else:
                status = (
                    "failed"
                    if failed
                    else "success"
                    if coverage.history_complete
                    else "partial_success"
                )
                session.execute(
                    update(job_runs)
                    .where(job_runs.c.id == run_id, job_runs.c.completed_at.is_(None))
                    .values(status=status, completed_at=self._now(), **values)
                )
            session.commit()
        except Exception:
            session.rollback()
            raise WalletHistoryError("wallet_history_audit_failed") from None
        finally:
            if owned:
                session.close()

    @staticmethod
    def _identity(payload: object) -> tuple[str | None, int | None]:
        """Minimum identity permits journaling before full transaction validation."""
        try:
            if not isinstance(payload, dict):
                return None, None
            slot = payload["slot"]
            signatures = payload["transaction"]["signatures"]
            if (
                type(slot) is not int
                or not 0 <= slot < 2**63
                or not isinstance(signatures, list)
                or not signatures
                or not isinstance(signatures[0], str)
            ):
                return None, None
            return _signature(signatures[0]), slot
        except (KeyError, TypeError, ValueError):
            return None, None

    def _quarantine(self, raw: RawEventRecord, code: str) -> None:
        try:
            self._outcomes.record_outcome(
                raw_event_id=raw.id,
                status=DecodeStatus.RAW_ONLY,
                safe_error_code=code,
                safe_error_message="wallet history observation could not be validated safely",
            )
        except DecodeOutcomeConflictError:
            # Reinspection cannot erase an outcome written by a later decoder.
            pass

    @staticmethod
    def _filter_reason(query: HistoryQuery, transaction: HistoryTransaction) -> str | None:
        if transaction.block_time is None:
            return "history_block_time_unavailable"
        if (
            not int(query.start_time.timestamp())
            <= transaction.block_time
            < int(query.end_time.timestamp())
        ):
            return "history_filter_mismatch"
        if query.status != "any" and query.status != transaction.status:
            return "history_filter_mismatch"
        if query.token_accounts == "none":
            payload = json.loads(transaction.payload_json)
            keys = list(payload["transaction"]["message"]["accountKeys"])
            loaded = payload["meta"].get("loadedAddresses")
            if isinstance(loaded, dict):
                keys.extend(loaded["writable"] + loaded["readonly"])
            if query.wallet not in keys:
                return "history_filter_mismatch"
        return None

    async def fetch(self, query: HistoryQuery) -> WalletHistoryResult:
        """Return known transactions and durable, explicit incompleteness reasons.

        The continuation token covers provider paging only. Transfer extraction
        and consumer progress must refer to the retained raw transaction rows.
        """
        if self._running:
            raise WalletHistoryError("wallet_history_already_running")
        query, settings = self._validate_request(query)
        started = self._now()
        self._running = True
        run_id = str(uuid4())
        page_ids: list[int] = []
        transaction_ids: list[int] = []
        transactions: list[HistoryTransaction] = []
        seen: dict[str, HistoryTransaction] = {}
        positions: dict[tuple[int, int], str] = {}
        last_position: tuple[int, int] | None = None
        tokens: set[str] = set()
        token = query.pagination_token
        reasons: list[str] = []
        warnings: list[str] = []
        transactions_seen = 0
        exhausted = False
        errored = False
        primary_error: str | None = None
        owned_rpc: HeliusHttpClient | None = None

        def incomplete(code: str, *, error: bool = True) -> None:
            nonlocal errored, primary_error
            if code not in reasons:
                reasons.append(code)
            errored |= error
            if error and primary_error is None:
                primary_error = code

        def coverage() -> HistoryCoverage:
            return HistoryCoverage(
                history_complete=exhausted and not reasons,
                exhausted=exhausted,
                transactions_seen=transactions_seen,
                transactions_returned=len(transactions),
                pages_fetched=len(page_ids),
                next_pagination_token=token,
                incomplete_reasons=tuple(reasons),
                warnings=tuple(warnings),
                state=EvidenceState.ERROR if errored else EvidenceState.OBSERVED,
                safe_error_code=primary_error,
            )

        if token is not None:
            tokens.add(token)
            incomplete("history_resumed", error=False)
        if query.token_accounts != "none" and query.start_time < _TOKEN_ACCOUNT_START:
            incomplete("token_account_history_unavailable", error=False)
            warnings.append("token_account_history_unavailable")
        try:
            self._audit(run_id, query, started, coverage=None, page_ids=[], transaction_ids=[])
            rpc = self._rpc
            if rpc is None:
                if settings.helius_api_key is None:
                    raise WalletHistoryError("wallet_history_requires_explicit_live_mode")
                owned_rpc = HeliusHttpClient(settings.helius_api_key, settings.helius_rpc_url)
                rpc = owned_rpc
            journal = RpcResponseJournal(
                self._source,
                Transport.HELIUS_RPC.value,
                "t021:helius-history-v1",
                notification_prefix="wallet-history-rpc",
                commitment=query.commitment,
            )
            while True:
                remaining = query.max_transactions - transactions_seen
                if remaining <= 0:
                    incomplete("history_limit_reached", error=False)
                    break
                if len(page_ids) >= self._max_pages:
                    incomplete("history_page_limit_reached", error=False)
                    break
                params = query.rpc_params(
                    limit=min(query.page_size, remaining), pagination_token=token
                )
                try:
                    response = await asyncio.wait_for(
                        rpc.rpc("getTransactionsForAddress", params), self._timeout
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    incomplete("wallet_history_rpc_failed")
                    break
                try:
                    page = journal.insert(
                        method="getTransactionsForAddress",
                        program_id=query.wallet,
                        params=params,
                        response=response,
                        received_at=self._now(),
                    )
                except Exception:
                    incomplete("wallet_history_persistence_failed")
                    break
                page_ids.append(page.id)
                try:
                    rows, next_token = parse_history_page(response, limit=params[1]["limit"])
                except HistoryValidationError:
                    incomplete("wallet_history_invalid_page")
                    self._quarantine(page, "wallet_history_invalid_page")
                    break
                transactions_seen += len(rows)
                invalid_page = False
                for payload in rows:
                    signature, slot = self._identity(payload)
                    try:
                        raw = journal.insert(
                            method="getTransactionsForAddress",
                            program_id=query.wallet,
                            params=params,
                            response=payload,
                            received_at=self._now(),
                            signature=signature,
                            slot=slot,
                            bare=True,
                        )
                    except Exception:
                        incomplete("wallet_history_persistence_failed")
                        invalid_page = True
                        break
                    transaction_ids.append(raw.id)
                    try:
                        transaction = parse_history_transaction(
                            payload, raw_event_id=raw.id, page_raw_event_id=page.id
                        )
                    except HistoryValidationError:
                        incomplete("wallet_history_invalid_transaction")
                        self._quarantine(raw, "wallet_history_invalid_transaction")
                        invalid_page = True
                        continue
                    reason = self._filter_reason(query, transaction)
                    existing = seen.get(transaction.signature)
                    position = (transaction.slot, transaction.transaction_index)
                    if reason is None and existing is not None:
                        if existing.payload_json != transaction.payload_json:
                            reason = "history_transaction_conflict"
                        else:
                            continue
                    if reason is None and position in positions:
                        reason = "history_position_conflict"
                    if reason is None and last_position is not None:
                        ordered = (
                            position > last_position
                            if query.sort_order == "asc"
                            else position < last_position
                        )
                        if not ordered:
                            reason = "history_order_invalid"
                    if reason is not None:
                        incomplete(reason)
                        self._quarantine(raw, reason)
                        invalid_page = True
                        continue
                    if query.token_accounts != "none" and transaction.slot < _TOKEN_ACCOUNT_SLOT:
                        incomplete("token_account_history_unavailable", error=False)
                        if "token_account_history_unavailable" not in warnings:
                            warnings.append("token_account_history_unavailable")
                    positions[position] = transaction.signature
                    last_position = position
                    seen[transaction.signature] = transaction
                    transactions.append(transaction)
                # Retrying this same page is safe; never skip a quarantined row
                # by exposing the provider's later continuation as committed.
                if invalid_page:
                    break
                if next_token is None:
                    token = None
                    exhausted = True
                    break
                if next_token in tokens:
                    incomplete("history_pagination_loop")
                    break
                tokens.add(next_token)
                token = next_token
            result_coverage = coverage()
            self._audit(
                run_id,
                query,
                started,
                coverage=result_coverage,
                page_ids=page_ids,
                transaction_ids=transaction_ids,
                failed=errored and not transactions,
            )
            return WalletHistoryResult(
                run_id, query, tuple(transactions), result_coverage, tuple(page_ids)
            )
        except asyncio.CancelledError:
            incomplete("history_cancelled")
            self._audit(
                run_id,
                query,
                started,
                coverage=coverage(),
                page_ids=page_ids,
                transaction_ids=transaction_ids,
                failed=True,
            )
            raise
        except WalletHistoryError:
            raise
        except Exception:
            incomplete("wallet_history_processing_failed")
            self._audit(
                run_id,
                query,
                started,
                coverage=coverage(),
                page_ids=page_ids,
                transaction_ids=transaction_ids,
                failed=True,
            )
            raise WalletHistoryError("wallet_history_processing_failed") from None
        finally:
            self._running = False
            if owned_rpc is not None:
                try:
                    await asyncio.wait_for(owned_rpc.close(), timeout=min(self._timeout, 10))
                except Exception:
                    pass
