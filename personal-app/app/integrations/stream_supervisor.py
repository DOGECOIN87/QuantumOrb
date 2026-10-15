"""Explicit stream supervision with durable recovery boundaries and bounded retries."""

from __future__ import annotations

import asyncio
import math
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal, Protocol

from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.stream_cursors import StreamCursor, StreamCursorRepository
from app.decoders import DecodeResult
from app.integrations.helius_http import HeliusHttpClient
from app.integrations.logs_subscribe import LogsSubscribeClient
from app.integrations.parsed_streams import ParsedStreamsClient
from app.integrations.stream_recovery import BoundedStreamRecovery, RecoveryReport
from app.logging_config import SafeError

ConnectionState = Literal[
    "disconnected", "connecting", "subscribed", "receiving", "reconnecting", "error"
]
ResultHandler = Callable[[DecodeResult], Awaitable[None]]


class StreamSupervisorError(SafeError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code, "Stream supervision operation failed", retryable=retryable)


class StreamClient(Protocol):
    async def start(self) -> None: ...

    async def receive_once(self) -> DecodeResult: ...

    async def close(self) -> None: ...


class RecoveryService(Protocol):
    async def recover(
        self, checkpoints: Sequence[StreamCursor], *, on_result: ResultHandler
    ) -> RecoveryReport: ...


@dataclass(frozen=True)
class SupervisorStatus:
    state: ConnectionState = "disconnected"
    retry_attempt: int = 0
    reconnects: int = 0
    data_gap: bool = False
    safe_error_code: str | None = None


class StreamSupervisor:
    """Run only the configured transport until cancelled or its retry budget ends.

    The result handler owns normalization/canonical persistence. It must retain
    canonical event-key deduplication: live and historical evidence can overlap.
    No socket, recovery task, or provider request starts on construction.
    """

    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session] | Callable[[], Session],
        *,
        client_factory: Callable[[], StreamClient] | None = None,
        recovery: RecoveryService | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        max_reconnect_attempts: int = 5,
        shutdown_timeout: float = 10,
    ) -> None:
        if type(max_reconnect_attempts) is not int or not 1 <= max_reconnect_attempts <= 5:
            raise StreamSupervisorError("invalid_stream_retry_budget")
        if (
            type(shutdown_timeout) not in {int, float}
            or not math.isfinite(shutdown_timeout)
            or shutdown_timeout <= 0
        ):
            raise StreamSupervisorError("invalid_stream_shutdown_timeout")
        self._settings = settings
        self._session_factory = session_factory
        self._client_factory = client_factory
        self._recovery = recovery
        self._clock = clock
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._jitter = jitter
        self._max_attempts = max_reconnect_attempts
        self._shutdown_timeout = shutdown_timeout
        self._cursors = StreamCursorRepository(session_factory)
        self._status = SupervisorStatus()
        self._running = False
        self._receiving_since: float | None = None
        self._delivery_lock = asyncio.Lock()

    @property
    def status(self) -> SupervisorStatus:
        return self._status

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise StreamSupervisorError("invalid_stream_clock")
        return value.astimezone(UTC)

    def _tick(self) -> float:
        value = self._monotonic()
        if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
            raise StreamSupervisorError("invalid_stream_monotonic_clock")
        return value

    def _programs(self) -> tuple[str, str]:
        return (self._settings.pump_program_id, self._settings.pump_amm_program_id)

    def _begin_recovery(self) -> tuple[StreamCursor, ...]:
        now = self._now()
        checkpoints = tuple(
            self._cursors.begin_recovery(self._settings.ingest_transport, program, now)
            for program in self._programs()
        )
        self._status = replace(
            self._status, data_gap=self._status.data_gap or any(c.data_gap for c in checkpoints)
        )
        return checkpoints

    def _mark_gap(self, reason: str) -> None:
        now = self._now()
        for program in self._programs():
            self._cursors.mark_gap(
                self._settings.ingest_transport, program, now=now, gap_reason=reason
            )
        self._status = replace(self._status, data_gap=True)

    def _stable_window(self) -> None:
        if self._receiving_since is not None and self._tick() - self._receiving_since >= 60:
            self._status = replace(self._status, retry_attempt=0)

    async def _deliver(self, result: DecodeResult, on_result: ResultHandler) -> None:
        async with self._delivery_lock:
            try:
                await on_result(result)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise StreamSupervisorError("stream_consumer_failed") from None

    async def _receive(self, client: StreamClient, on_result: ResultHandler) -> None:
        while True:
            result = await client.receive_once()
            tick = self._tick()
            if self._receiving_since is None:
                self._receiving_since = tick
            self._stable_window()
            self._status = replace(self._status, state="receiving", safe_error_code=None)
            await self._deliver(result, on_result)

    async def _session(
        self,
        client: StreamClient,
        recovery: RecoveryService,
        checkpoints: Sequence[StreamCursor],
        on_result: ResultHandler,
    ) -> None:
        await client.start()
        self._status = replace(self._status, state="subscribed", safe_error_code=None)
        self._receiving_since = None

        async def recover() -> RecoveryReport:
            return await recovery.recover(
                checkpoints, on_result=lambda result: self._deliver(result, on_result)
            )

        live = asyncio.create_task(self._receive(client, on_result))
        backfill = asyncio.create_task(recover())
        try:
            done, _ = await asyncio.wait((live, backfill), return_when=asyncio.FIRST_COMPLETED)
            # A simultaneous socket failure must not hide a permanent consumer
            # failure raised by historical delivery.
            if backfill in done and backfill.exception() is not None:
                await backfill
            if live in done:
                await live
                raise StreamSupervisorError("stream_receiver_stopped", retryable=True)
            report = await backfill
            self._status = replace(self._status, data_gap=self._status.data_gap or report.data_gap)
            await live
        finally:
            for task in (live, backfill):
                if not task.done():
                    task.cancel()
            await asyncio.gather(live, backfill, return_exceptions=True)

    async def _close_client(self, client: StreamClient) -> None:
        try:
            await asyncio.wait_for(client.close(), timeout=self._shutdown_timeout)
        except Exception:
            # Cleanup diagnostics must not replace the recovery/consumer failure.
            pass

    async def run(self, on_result: ResultHandler) -> None:
        """Explicitly run intake and recovery; cancellation preserves a durable gap."""
        if self._running:
            raise StreamSupervisorError("stream_supervisor_already_running")
        settings = self._settings
        if (
            settings.ingest_mode != "live"
            or settings.ingest_transport not in {"parsed_streams", "logs_subscribe"}
            or settings.solana_network != "mainnet-beta"
            or settings.helius_api_key is None
        ):
            raise StreamSupervisorError("stream_requires_explicit_live_mode")
        try:
            settings = Settings.model_validate(settings.model_dump(warnings=False))
        except Exception:
            raise StreamSupervisorError("invalid_stream_settings") from None
        self._running = True
        self._status = SupervisorStatus(state="connecting")
        self._receiving_since = None
        owned_rpc: HeliusHttpClient | None = None
        client: StreamClient | None = None
        try:
            recovery = self._recovery
            if recovery is None:
                if settings.helius_api_key is None:
                    raise StreamSupervisorError("stream_requires_explicit_live_mode")
                owned_rpc = HeliusHttpClient(settings.helius_api_key, settings.helius_rpc_url)
                recovery = BoundedStreamRecovery(settings, self._session_factory, rpc=owned_rpc)
            while True:
                checkpoints = self._begin_recovery()
                try:
                    if self._client_factory is not None:
                        client = self._client_factory()
                    elif settings.ingest_transport == "parsed_streams":
                        client = ParsedStreamsClient(settings, self._session_factory)
                    else:
                        client = LogsSubscribeClient(settings, self._session_factory)
                    await self._session(client, recovery, checkpoints, on_result)
                except asyncio.CancelledError:
                    self._mark_gap("stream_cancelled")
                    self._status = replace(self._status, state="disconnected")
                    raise
                except Exception as error:
                    self._receiving_since = None
                    retryable = (
                        error.retryable
                        if isinstance(error, SafeError)
                        else isinstance(error, (TimeoutError, OSError))
                    )
                    self._begin_recovery()
                    safe = (
                        error
                        if isinstance(error, StreamSupervisorError)
                        else StreamSupervisorError(
                            "stream_connection_failed"
                            if retryable
                            else "stream_connection_rejected",
                            retryable=retryable,
                        )
                    )
                    self._status = replace(self._status, safe_error_code=safe.code)
                    if not retryable:
                        self._mark_gap("stream_stopped")
                        self._status = replace(self._status, state="error")
                        raise safe from None
                    if self._status.retry_attempt >= self._max_attempts:
                        self._mark_gap("stream_reconnect_exhausted")
                        self._status = replace(
                            self._status,
                            state="error",
                            safe_error_code="stream_reconnect_exhausted",
                        )
                        raise StreamSupervisorError("stream_reconnect_exhausted") from None
                finally:
                    if client is not None:
                        await self._close_client(client)
                        client = None
                attempt = self._status.retry_attempt + 1
                self._status = replace(
                    self._status,
                    state="reconnecting",
                    retry_attempt=attempt,
                    reconnects=self._status.reconnects + 1,
                )
                base = min(2 ** (attempt - 1), 30)
                delay = self._jitter(0.75 * base, 1.25 * base)
                if (
                    type(delay) not in {int, float}
                    or not math.isfinite(delay)
                    or not 0.75 * base <= delay <= 1.25 * base
                ):
                    raise StreamSupervisorError("invalid_stream_jitter")
                await self._sleeper(delay)
        except asyncio.CancelledError:
            self._mark_gap("stream_cancelled")
            self._status = replace(self._status, state="disconnected")
            raise
        except Exception as error:
            self._status = replace(self._status, data_gap=True)
            try:
                self._mark_gap("stream_supervisor_failed")
            except Exception:
                # A database/clock failure can prevent the durable gap write;
                # keep the original failure visible and never report success.
                pass
            self._status = replace(
                self._status,
                state="error",
                safe_error_code=error.code
                if isinstance(error, SafeError)
                else "stream_supervisor_failed",
            )
            if isinstance(error, StreamSupervisorError):
                raise
            raise StreamSupervisorError("stream_supervisor_failed") from None
        finally:
            self._running = False
            if client is not None:
                await self._close_client(client)
            if owned_rpc is not None:
                try:
                    await asyncio.wait_for(owned_rpc.close(), timeout=self._shutdown_timeout)
                except Exception:
                    pass
