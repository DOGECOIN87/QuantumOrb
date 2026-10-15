"""Bounded, journal-first RPC reconciliation for an explicitly selected stream.

Coverage is a proof about the listing boundary and successful delivery, rather
than a count of decoded raw rows. Same-slot signatures have no lexical ordering.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import insert, update

from app.config import Settings
from app.db.catalog import load_pinned_catalog
from app.db.decode_outcomes import DecodeOutcomeConflictError, DecodeOutcomeRepository
from app.db.models import job_runs
from app.db.raw_journal import RawEventRecord, RawJournal, SessionSource
from app.db.rpc_journal import RpcJournalError
from app.db.rpc_journal import RpcResponseJournal as RpcResponseJournal
from app.db.stream_cursors import StreamCursor, StreamCursorRepository
from app.decoders import Decoder, DecodeResult, DecoderInput
from app.domain.enums import DecodeStatus
from app.domain.models import _signature
from app.integrations.logs_subscribe import RpcReader
from app.integrations.raw_logs_decoder import RawLogsDecoder


@dataclass(frozen=True)
class ProgramRecovery:
    """Coverage of one monitored address in one bounded recovery run."""

    program_id: str
    covered: bool
    listed: int
    hydrated: int
    delivered: int
    excluded: int
    gap_reason: str | None

    @property
    def data_gap(self) -> bool:
        return not self.covered


@dataclass(frozen=True)
class RecoveryReport:
    """Immutable run result; prior persisted gaps remain independently visible."""

    run_id: str
    programs: tuple[ProgramRecovery, ...]

    @property
    def data_gap(self) -> bool:
        return any(program.data_gap for program in self.programs)


@dataclass(frozen=True)
class _Entry:
    signature: str
    slot: int
    block_time: int | None
    failed: bool


@dataclass
class _Progress:
    program_id: str
    listed: int = 0
    hydrated: int = 0
    delivered: int = 0
    excluded: int = 0

    def result(self, *, covered: bool = False, gap: str | None = None) -> ProgramRecovery:
        return ProgramRecovery(
            self.program_id,
            covered and gap is None,
            self.listed,
            self.hydrated,
            self.delivered,
            self.excluded,
            gap,
        )


class _Gap(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BoundedStreamRecovery:
    """Backfill at most 500 listed candidates and 120 seconds per run.

    HTTP calls are bounded independently. Consumers own canonical persistence;
    successful decode status alone never certifies successful consumer delivery.
    """

    def __init__(
        self,
        settings: Settings,
        session_factory: SessionSource,
        *,
        rpc: RpcReader,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_signatures: int = 500,
        window_seconds: int = 120,
        page_size: int | None = None,
        rpc_timeout: float = 45,
        decoder: Decoder | None = None,
    ) -> None:
        if settings.ingest_transport not in {"logs_subscribe", "parsed_streams"}:
            raise ValueError("recovery requires an explicitly selected stream transport")
        if settings.ingest_mode != "live" or settings.solana_network != "mainnet-beta":
            raise ValueError("recovery requires live mainnet configuration")
        for value, maximum in ((max_signatures, 500), (window_seconds, 120)):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("recovery bound is outside the supported range")
        selected_page_size = settings.backfill_page_size if page_size is None else page_size
        if type(selected_page_size) is not int or not 1 <= selected_page_size <= 1000:
            raise ValueError("recovery page size is outside the supported range")
        if (
            isinstance(rpc_timeout, bool)
            or not isinstance(rpc_timeout, int | float)
            or not math.isfinite(rpc_timeout)
            or rpc_timeout <= 0
        ):
            raise ValueError("recovery timeout must be finite and positive")
        self._settings = settings
        self._source = session_factory
        self._rpc = rpc
        self._clock = clock
        self._maximum = max_signatures
        self._window = window_seconds
        self._page_size = selected_page_size
        self._timeout = rpc_timeout
        self._decoder = decoder or RawLogsDecoder(
            settings, accepted_transport=settings.ingest_transport
        )
        self._journal = RpcResponseJournal(
            session_factory,
            settings.ingest_transport,
            "t020:" + load_pinned_catalog(settings)[0].idl_revision,
        )
        self._outcomes = DecodeOutcomeRepository(session_factory)
        self._cursors = StreamCursorRepository(session_factory)
        self._running = False

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("recovery clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    def _audit(
        self,
        run_id: str,
        started_at: datetime,
        programs: Sequence[ProgramRecovery],
        *,
        finished: bool,
        error: str | None = None,
    ) -> None:
        journal = RawJournal(self._source)
        session, owned = journal._session()
        try:
            counts = {"programs": [asdict(program) for program in programs]}
            values = {
                "counts_json": json.dumps(counts, sort_keys=True, separators=(",", ":")),
                "status": (
                    "failed"
                    if error is not None
                    else "partial_success"
                    if any(program.data_gap for program in programs)
                    else "success"
                )
                if finished
                else "running",
                "safe_error_code": error,
                "safe_error_message": "stream reconciliation did not complete safely"
                if error
                else None,
            }
            if finished:
                session.execute(
                    update(job_runs)
                    .where(job_runs.c.id == run_id, job_runs.c.completed_at.is_(None))
                    .values(**values, completed_at=self._now())
                )
            else:
                session.execute(
                    insert(job_runs).values(
                        id=run_id,
                        job_name="stream_reconciliation",
                        started_at=started_at,
                        completed_at=None,
                        **values,
                    )
                )
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            if owned:
                session.close()

    async def _call(
        self, method: str, params: list[Any], program_id: str
    ) -> tuple[Any, RawEventRecord]:
        try:
            response = await asyncio.wait_for(self._rpc.rpc(method, params), self._timeout)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise _Gap("recovery_rpc_failed") from None
        try:
            raw = self._journal.insert(
                method=method,
                program_id=program_id,
                params=params,
                response=response,
                received_at=self._now(),
            )
        except RpcJournalError as error:
            raise _Gap(error.code) from None
        return response, raw

    @staticmethod
    def _entries(response: Any, limit: int) -> list[_Entry]:
        if not isinstance(response, dict) or set(response) != {"result"}:
            raise _Gap("invalid_signature_response")
        rows = response["result"]
        if not isinstance(rows, list) or len(rows) > limit:
            raise _Gap("invalid_signature_response")
        entries: list[_Entry] = []
        allowed = {"signature", "slot", "err", "memo", "blockTime", "confirmationStatus"}
        for row in rows:
            if (
                not isinstance(row, dict)
                or set(row) - allowed
                or not {"signature", "slot", "err", "blockTime"} <= set(row)
                or type(row["slot"]) is not int
                or row["slot"] < 0
                or (
                    row["blockTime"] is not None
                    and (type(row["blockTime"]) is not int or row["blockTime"] < 0)
                )
                or row.get("confirmationStatus") not in (None, "confirmed", "finalized")
                or not isinstance(row["signature"], str)
                or (row["err"] is not None and not isinstance(row["err"], dict | str))
                or (row.get("memo") is not None and not isinstance(row["memo"], str))
            ):
                raise _Gap("invalid_signature_response")
            try:
                signature = _signature(row["signature"])
            except (TypeError, ValueError):
                raise _Gap("invalid_signature_response") from None
            entries.append(_Entry(signature, row["slot"], row["blockTime"], row["err"] is not None))
        return entries

    def _record(self, result: DecodeResult) -> None:
        try:
            self._outcomes.record_outcome(
                raw_event_id=result.raw_event_id,
                status=result.status,
                safe_error_code=result.safe_error_code,
                safe_error_message=result.safe_error_message,
            )
        except DecodeOutcomeConflictError:
            if result.status == DecodeStatus.DECODED:
                raise

    @staticmethod
    def _raw_only(raw: RawEventRecord, code: str) -> DecodeResult:
        return DecodeResult(
            raw_event_id=raw.id,
            status=DecodeStatus.RAW_ONLY,
            safe_error_code=code,
            safe_error_message="recovery transaction could not be decoded safely",
        )

    async def _hydrate(self, entry: _Entry, program_id: str) -> DecodeResult:
        params = [
            entry.signature,
            {"commitment": "confirmed", "encoding": "json", "maxSupportedTransactionVersion": 1},
        ]
        response, response_raw = await self._call("getTransaction", params, program_id)
        if (
            not isinstance(response, dict)
            or set(response) != {"result"}
            or not isinstance(response["result"], dict)
        ):
            result = self._raw_only(response_raw, "recovery_transaction_unavailable")
            self._record(result)
            return result
        transaction = response["result"]
        try:
            raw = self._journal.insert(
                method="getTransaction",
                program_id=program_id,
                params=params,
                response=transaction,
                received_at=self._now(),
                signature=entry.signature,
                slot=entry.slot,
                bare=True,
            )
        except RpcJournalError as error:
            raise _Gap(error.code) from None
        tx = transaction.get("transaction")
        signatures = tx.get("signatures") if isinstance(tx, dict) else None
        meta = transaction.get("meta")
        if (
            type(transaction.get("slot")) is not int
            or transaction["slot"] != entry.slot
            or not isinstance(signatures, list)
            or not signatures
            or signatures[0] != entry.signature
        ):
            result = self._raw_only(raw, "recovery_transaction_identity_mismatch")
        elif not isinstance(meta, dict) or "err" not in meta:
            result = self._raw_only(raw, "recovery_transaction_metadata_unavailable")
        elif meta["err"] is not None and not isinstance(meta["err"], dict | str):
            result = self._raw_only(raw, "recovery_transaction_metadata_unavailable")
        elif meta["err"] is not None:
            result = self._raw_only(raw, "failed_transaction")
        else:
            try:
                decoded = self._decoder.decode(
                    DecoderInput(
                        raw_event_id=raw.id,
                        transport=raw.transport,
                        payload_json=raw.payload_json,
                    )
                )
                result = DecodeResult.model_validate(decoded.model_dump(warnings=False))
                if result.raw_event_id != raw.id:
                    raise ValueError
            except Exception:
                result = self._raw_only(raw, "recovery_transaction_decode_failed")
        self._record(result)
        return result

    async def _program(
        self,
        checkpoint: StreamCursor,
        cutoff: datetime,
        budget: list[int],
        cached: dict[str, tuple[int, DecodeResult]],
        on_result: Callable[[DecodeResult], Awaitable[None]],
        progress: _Progress,
    ) -> ProgramRecovery:
        gap: str | None = None
        before: str | None = None
        seen: set[str] = set()
        previous_slot: int | None = None
        anchor_slot = checkpoint.anchor_slot if checkpoint.pending_since else checkpoint.slot
        anchor_signature = (
            checkpoint.anchor_signature if checkpoint.pending_since else checkpoint.signature
        )
        has_anchor = anchor_slot is not None and anchor_signature is not None
        anchor_seen = False
        covered = False
        # For a quiet session, the last frozen watch/coverage timestamp can be
        # older than the latest reconnect request. Keep that older boundary.
        pending = checkpoint.anchor_received_at if checkpoint.pending_since else None
        while not covered:
            if budget[0] == 0:
                gap = gap or "recovery_signature_limit"
                break
            limit = min(self._page_size, budget[0])
            options: dict[str, Any] = {"commitment": "confirmed", "limit": limit}
            if before is not None:
                options["before"] = before
            response, response_raw = await self._call(
                "getSignaturesForAddress", [checkpoint.program_id, options], checkpoint.program_id
            )
            try:
                entries = self._entries(response, limit)
            except _Gap as error:
                self._record(self._raw_only(response_raw, error.code))
                raise
            budget[0] -= len(entries)
            progress.listed += len(entries)
            terminal = len(entries) < limit
            reached_boundary = False
            for entry in entries:
                if entry.signature in seen or (
                    previous_slot is not None and entry.slot > previous_slot
                ):
                    raise _Gap("recovery_invalid_pagination")
                seen.add(entry.signature)
                previous_slot = entry.slot
                # Slot boundary is authoritative; same-slot signatures are all
                # inspected regardless of their relative listing positions.
                if has_anchor and entry.slot < anchor_slot:  # type: ignore[operator]
                    if anchor_seen:
                        covered = True
                    else:
                        gap = gap or "recovery_anchor_missing"
                    reached_boundary = True
                    break
                is_anchor = has_anchor and entry.signature == anchor_signature
                if is_anchor:
                    if entry.slot != anchor_slot:
                        raise _Gap("recovery_anchor_identity_mismatch")
                    anchor_seen = True
                    # A cursor proves receipt, not successful consumer commit.
                    # Replay within the window or retain an explicit gap.
                if entry.block_time is None:
                    gap = gap or "recovery_block_time_unavailable"
                    continue
                if entry.block_time < cutoff.timestamp():
                    gap = gap or (
                        "recovery_anchor_outside_window" if is_anchor else "recovery_time_limit"
                    )
                    reached_boundary = True
                    break
                if (
                    not has_anchor
                    and pending
                    and entry.block_time < math.floor(pending.timestamp())
                ):
                    covered = True
                    break
                if entry.failed:
                    progress.excluded += 1
                    continue
                cached_result = cached.get(entry.signature)
                if cached_result is not None:
                    if cached_result[0] != entry.slot:
                        raise _Gap("recovery_transaction_identity_mismatch")
                    result = cached_result[1]
                else:
                    progress.hydrated += 1
                    result = await self._hydrate(entry, checkpoint.program_id)
                    cached[entry.signature] = (entry.slot, result)
                    if result.status == DecodeStatus.DECODED:
                        # A consumer exception is propagated after run audit;
                        # raw decode status is not evidence of this delivery.
                        await on_result(result)
                        progress.delivered += 1
                if result.status != DecodeStatus.DECODED:
                    if result.safe_error_code in {"no_supported_event", "failed_transaction"}:
                        progress.excluded += 1
                    else:
                        gap = gap or "recovery_decode_incomplete"
            if (
                reached_boundary
                or covered
                or (gap is not None and gap in {"recovery_time_limit", "recovery_anchor_missing"})
            ):
                break
            if terminal:
                if has_anchor:
                    covered = anchor_seen
                    if not anchor_seen:
                        gap = gap or "recovery_anchor_missing"
                elif pending and pending >= cutoff:
                    covered = True
                else:
                    gap = gap or "recovery_no_baseline"
                break
            before = entries[-1].signature
        if not has_anchor and (pending is None or pending < cutoff):
            gap = gap or "recovery_no_baseline"
        if gap is None and not covered:
            gap = "recovery_incomplete"
        return progress.result(covered=covered, gap=gap)

    async def recover(
        self,
        checkpoints: Sequence[StreamCursor],
        *,
        on_result: Callable[[DecodeResult], Awaitable[None]],
    ) -> RecoveryReport:
        """Recover one frozen set of anchors without changing current live cursors."""
        if self._running:
            raise ValueError("a recovery is already running")
        allowed = {self._settings.pump_program_id, self._settings.pump_amm_program_id}
        identities = [(item.transport, item.program_id) for item in checkpoints]
        if (
            not checkpoints
            or len(identities) != len(set(identities))
            or any(
                transport != self._settings.ingest_transport or program not in allowed
                for transport, program in identities
            )
        ):
            raise ValueError("recovery checkpoints do not match selected monitored programs")
        started = self._now()
        cutoff = started - timedelta(seconds=self._window)
        run_id = str(uuid4())
        programs: list[ProgramRecovery] = []
        budget = [self._maximum]
        cached: dict[str, tuple[int, DecodeResult]] = {}
        progress: _Progress | None = None
        self._running = True
        try:
            self._audit(run_id, started, programs, finished=False)
            for checkpoint in checkpoints:
                progress = _Progress(checkpoint.program_id)
                try:
                    program = await self._program(
                        checkpoint, cutoff, budget, cached, on_result, progress
                    )
                except _Gap as error:
                    program = progress.result(gap=error.code)
                programs.append(program)
                progress = None
                self._cursors.finish_recovery(
                    checkpoint.transport,
                    checkpoint.program_id,
                    now=self._now(),
                    gap_reason=program.gap_reason,
                )
            self._audit(run_id, started, programs, finished=True)
            return RecoveryReport(run_id, tuple(programs))
        except asyncio.CancelledError:
            if progress is not None:
                programs.append(progress.result(gap="recovery_interrupted"))
            self._audit(run_id, started, programs, finished=True, error="recovery_interrupted")
            raise
        except BaseException:
            if progress is not None:
                programs.append(progress.result(gap="recovery_consumer_failed"))
            self._audit(run_id, started, programs, finished=True, error="recovery_consumer_failed")
            raise
        finally:
            self._running = False
