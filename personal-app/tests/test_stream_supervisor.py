"""Deterministic connection, retry, cancellation, and concurrent recovery tests."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import UTC, datetime

import pytest

from app.config import Settings
from app.db.session import create_all, create_database_engine, create_session_factory
from app.db.stream_cursors import StreamCursorRepository
from app.decoders import DecodeResult
from app.domain.enums import DecodeStatus
from app.integrations import stream_supervisor as module
from app.integrations.stream_recovery import RecoveryReport
from app.integrations.stream_supervisor import StreamSupervisor, StreamSupervisorError
from app.logging_config import SafeError

NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)
SECRET = "synthetic-supervisor-secret"


def settings(**overrides):
    return Settings(
        _env_file=None,
        **{
            "ingest_mode": "live",
            "ingest_transport": "logs_subscribe",
            "helius_api_key": SECRET,
            **overrides,
        },
    )


def result(raw_id=1):
    return DecodeResult(
        raw_event_id=raw_id,
        status=DecodeStatus.RAW_ONLY,
        safe_error_code="no_supported_event",
        safe_error_message="retained test observation",
    )


def transient():
    return SafeError("stream_reset", "connection interrupted", retryable=True)


def permanent():
    return SafeError("invalid_stream_message", "message rejected")


@pytest.fixture
def session_factory(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'supervisor.db'}")
    create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


class Client:
    def __init__(self, actions=(), *, start_error=None, on_start=None):
        self.actions = deque(actions)
        self.start_error = start_error
        self.on_start = on_start
        self.starts = 0
        self.closes = 0
        self.waiting = asyncio.Event()

    async def start(self):
        self.starts += 1
        if self.on_start:
            self.on_start()
        if self.start_error:
            raise self.start_error

    async def receive_once(self):
        await asyncio.sleep(0)
        self.waiting.set()
        if not self.actions:
            await asyncio.Event().wait()
        item = self.actions.popleft()
        if callable(item):
            item = item()
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self):
        self.closes += 1


class Recovery:
    def __init__(self, factory):
        self.repo = StreamCursorRepository(factory)
        self.checkpoints = []
        self.entered = asyncio.Event()
        self.release = None
        self.output = None
        self.error = None
        self.cancelled = False

    async def recover(self, checkpoints, *, on_result):
        self.checkpoints.append(checkpoints)
        self.entered.set()
        try:
            if self.release is not None:
                await self.release.wait()
            if self.error is not None:
                raise self.error
            if self.output is not None:
                await on_result(self.output)
            for checkpoint in checkpoints:
                self.repo.finish_recovery(
                    checkpoint.transport, checkpoint.program_id, now=NOW, gap_reason=None
                )
            return RecoveryReport("test-run", ())
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class Harness:
    def __init__(self, factory, clients, **options):
        self.clients = deque(clients)
        self.created = []
        self.sleeps = []
        self.output = []
        self.recovery = Recovery(factory)
        self.ticks = 0

        def client_factory():
            client = self.clients.popleft()
            self.created.append(client)
            return client

        async def sleeper(delay):
            self.sleeps.append(delay)
            await asyncio.sleep(0)

        self.supervisor = StreamSupervisor(
            settings(),
            factory,
            client_factory=client_factory,
            recovery=self.recovery,
            clock=lambda: NOW,
            monotonic=lambda: self.ticks,
            sleeper=sleeper,
            jitter=lambda low, high: (low + high) / 2,
            **options,
        )

    async def consume(self, decoded):
        self.output.append(decoded)
        await asyncio.sleep(0)

    async def fail(self, code=None):
        with pytest.raises(StreamSupervisorError) as raised:
            await self.supervisor.run(self.consume)
        assert SECRET not in str(raised.value)
        if code:
            assert raised.value.code == code
        return raised.value


def test_constructor_is_inert_and_run_requires_explicit_live_stream(session_factory):
    supervisor = StreamSupervisor(Settings(_env_file=None), session_factory)
    assert supervisor.status.state == "disconnected"
    with pytest.raises(StreamSupervisorError, match="Stream supervision") as raised:
        asyncio.run(supervisor.run(lambda decoded: asyncio.sleep(0)))
    assert raised.value.code == "stream_requires_explicit_live_mode"


def test_transient_disconnect_resubscribes_with_same_factory_and_keeps_gap_anchor(session_factory):
    clients = [Client([result(1), transient()]), Client([result(2), permanent()])]
    harness = Harness(session_factory, clients)
    asyncio.run(harness.fail("stream_connection_rejected"))
    assert harness.sleeps == [1]
    assert [client.starts for client in clients] == [1, 1]
    assert [client.closes for client in clients] == [1, 1]
    assert [decoded.raw_event_id for decoded in harness.output] == [1, 2]
    assert len(harness.recovery.checkpoints) == 2
    assert harness.supervisor.status.reconnects == 1
    repo = StreamCursorRepository(session_factory)
    for program in (settings().pump_program_id, settings().pump_amm_program_id):
        cursor = repo.load("logs_subscribe", program)
        assert cursor.data_gap and cursor.gap_reason == "stream_stopped"


def test_five_reconnect_attempts_bound_flapping_connections(session_factory):
    clients = [Client(start_error=transient()) for _ in range(6)]
    harness = Harness(session_factory, clients)
    asyncio.run(harness.fail("stream_reconnect_exhausted"))
    assert harness.sleeps == [1, 2, 4, 8, 16]
    assert len(harness.created) == 6
    assert all(client.closes == 1 for client in clients)
    assert harness.supervisor.status.state == "error"
    assert harness.supervisor.status.data_gap


def test_unstable_receiving_does_not_reset_retry_budget(session_factory):
    clients = [Client([result(), transient()]) for _ in range(3)]
    harness = Harness(session_factory, clients, max_reconnect_attempts=2)
    asyncio.run(harness.fail("stream_reconnect_exhausted"))
    assert harness.sleeps == [1, 2]
    assert len(harness.output) == 3


def test_only_sixty_seconds_of_receiving_resets_retry_budget_once(session_factory):
    clients = [Client(start_error=transient()), Client(), Client(start_error=transient())]
    harness = Harness(session_factory, clients, max_reconnect_attempts=1)

    def after_stable_window():
        harness.ticks = 60
        return result(2)

    clients[1].actions.extend([result(), after_stable_window, transient()])
    asyncio.run(harness.fail("stream_reconnect_exhausted"))
    assert harness.sleeps == [1, 1]
    assert len(harness.created) == 3


@pytest.mark.parametrize("error", [permanent(), RuntimeError(SECRET)])
def test_permanent_and_unexpected_errors_never_retry_or_switch_transport(session_factory, error):
    client = Client(start_error=error)
    harness = Harness(session_factory, [client])
    asyncio.run(harness.fail("stream_connection_rejected"))
    assert harness.sleeps == []
    assert harness.supervisor.status.data_gap


def test_recovery_does_not_block_live_intake_and_cancellation_is_durable(session_factory):
    client = Client([result()])
    harness = Harness(session_factory, [client])
    harness.recovery.release = asyncio.Event()

    async def scenario():
        task = asyncio.create_task(harness.supervisor.run(harness.consume))
        await asyncio.wait_for(harness.recovery.entered.wait(), timeout=1)
        for _ in range(100):
            if harness.output:
                break
            await asyncio.sleep(0)
        assert harness.output
        assert harness.supervisor.status.state == "receiving"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert harness.recovery.cancelled
    assert client.closes == 1
    assert harness.supervisor.status.state == "disconnected"
    cursor = StreamCursorRepository(session_factory).load(
        "logs_subscribe", settings().pump_program_id
    )
    assert cursor.pending_since is not None
    assert cursor.data_gap and cursor.gap_reason == "stream_cancelled"


def test_recovery_results_use_the_same_serialized_consumer(session_factory):
    harness = Harness(session_factory, [Client()])
    harness.recovery.output = result(42)

    async def scenario():
        delivered = asyncio.Event()

        async def consume(decoded):
            harness.output.append(decoded)
            delivered.set()

        task = asyncio.create_task(harness.supervisor.run(consume))
        await asyncio.wait_for(delivered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert [decoded.raw_event_id for decoded in harness.output] == [42]


def test_consumer_failure_is_permanent_and_preserves_pending_gap(session_factory):
    harness = Harness(session_factory, [Client([result()])])

    async def consume(decoded):
        raise RuntimeError(SECRET)

    async def scenario():
        with pytest.raises(StreamSupervisorError) as raised:
            await harness.supervisor.run(consume)
        assert raised.value.code == "stream_consumer_failed"
        assert SECRET not in str(raised.value)

    asyncio.run(scenario())
    assert harness.sleeps == []
    assert harness.supervisor.status.data_gap


def test_recovery_failure_closes_live_receiver_and_stops_safely(session_factory):
    client = Client()
    harness = Harness(session_factory, [client])
    harness.recovery.error = RuntimeError(SECRET)
    asyncio.run(harness.fail("stream_connection_rejected"))
    assert client.closes == 1
    assert harness.supervisor.status.data_gap


def test_second_run_is_rejected_while_first_is_active(session_factory):
    client = Client()
    harness = Harness(session_factory, [client])

    async def scenario():
        task = asyncio.create_task(harness.supervisor.run(harness.consume))
        await asyncio.wait_for(client.waiting.wait(), timeout=1)
        with pytest.raises(StreamSupervisorError) as raised:
            await harness.supervisor.run(harness.consume)
        assert raised.value.code == "stream_supervisor_already_running"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_cancellation_during_backoff_preserves_anchor_and_closes_session(session_factory):
    client = Client(start_error=transient())
    harness = Harness(session_factory, [client])

    async def scenario():
        sleeping = asyncio.Event()

        async def sleeper(delay):
            sleeping.set()
            await asyncio.Event().wait()

        harness.supervisor._sleeper = sleeper
        task = asyncio.create_task(harness.supervisor.run(harness.consume))
        await asyncio.wait_for(sleeping.wait(), timeout=1)
        assert harness.supervisor.status.state == "reconnecting"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert client.closes == 1
    cursor = StreamCursorRepository(session_factory).load(
        "logs_subscribe", settings().pump_program_id
    )
    assert cursor.pending_since is not None
    assert cursor.data_gap and cursor.gap_reason == "stream_cancelled"


@pytest.mark.parametrize("close_error", [None, RuntimeError(SECRET)])
def test_default_recovery_closes_owned_http_resource_without_hiding_failure(
    session_factory, monkeypatch, close_error
):
    class Rpc:
        closed = 0

        async def close(self):
            self.closed += 1
            if close_error:
                raise close_error

    rpc = Rpc()
    constructed = []

    def make_rpc(*args):
        constructed.append(args)
        return rpc

    def recovery_factory(*args, **kwargs):
        assert kwargs["rpc"] is rpc
        return Recovery(session_factory)

    monkeypatch.setattr(module, "HeliusHttpClient", make_rpc)
    monkeypatch.setattr(module, "BoundedStreamRecovery", recovery_factory)
    supervisor = StreamSupervisor(
        settings(),
        session_factory,
        client_factory=lambda: Client(start_error=permanent()),
        clock=lambda: NOW,
    )
    with pytest.raises(StreamSupervisorError) as raised:
        asyncio.run(supervisor.run(lambda decoded: asyncio.sleep(0)))
    assert raised.value.code == "stream_connection_rejected"
    assert SECRET not in str(raised.value)
    assert len(constructed) == 1
    assert rpc.closed == 1


@pytest.mark.parametrize("value", [0, 6, True, "5"])
def test_invalid_retry_budget_is_rejected(session_factory, value):
    with pytest.raises(StreamSupervisorError):
        StreamSupervisor(settings(), session_factory, max_reconnect_attempts=value)


@pytest.mark.parametrize("value", [0, -1, True, float("nan"), float("inf"), "10"])
def test_invalid_shutdown_timeout_is_rejected(session_factory, value):
    with pytest.raises(StreamSupervisorError):
        StreamSupervisor(settings(), session_factory, shutdown_timeout=value)


@pytest.mark.parametrize("value", [-1, True, float("nan"), float("inf")])
def test_invalid_jitter_is_rejected_and_connection_closed(session_factory, value):
    harness = Harness(session_factory, [Client(start_error=transient())])
    harness.supervisor._jitter = lambda low, high: value
    asyncio.run(harness.fail("invalid_stream_jitter"))
    assert harness.created[0].closes == 1
    cursor = StreamCursorRepository(session_factory).load(
        "logs_subscribe", settings().pump_program_id
    )
    assert cursor.data_gap


def test_failed_backoff_sleeper_persists_terminal_gap(session_factory):
    harness = Harness(session_factory, [Client(start_error=transient())])

    async def sleeper(delay):
        raise RuntimeError(SECRET)

    harness.supervisor._sleeper = sleeper
    asyncio.run(harness.fail("stream_supervisor_failed"))
    cursor = StreamCursorRepository(session_factory).load(
        "logs_subscribe", settings().pump_program_id
    )
    assert cursor.data_gap


def test_slow_failure_cleanup_cannot_certify_a_stable_receiving_window(session_factory):
    clients = [Client([result(), transient()]), Client([result(), transient()])]
    harness = Harness(session_factory, clients, max_reconnect_attempts=1)

    def delayed_transport_error():
        harness.ticks += 60
        return transient()

    clients[0].actions = deque([result(), delayed_transport_error])
    asyncio.run(harness.fail("stream_reconnect_exhausted"))
    assert harness.sleeps == [1]


def test_backfill_consumer_failure_wins_over_simultaneous_transient_disconnect(session_factory):
    client = Client([transient()])
    harness = Harness(session_factory, [client])
    harness.recovery.error = StreamSupervisorError("stream_consumer_failed")
    asyncio.run(harness.fail("stream_consumer_failed"))
    assert harness.sleeps == []


@pytest.mark.parametrize(
    "transport,class_name",
    [
        ("logs_subscribe", "LogsSubscribeClient"),
        ("parsed_streams", "ParsedStreamsClient"),
    ],
)
def test_default_factory_uses_only_the_configured_transport(
    session_factory, monkeypatch, transport, class_name
):
    calls = []

    def factory(*args):
        calls.append(args)
        return Client(start_error=permanent())

    monkeypatch.setattr(module, class_name, factory)
    supervisor = StreamSupervisor(
        settings(ingest_transport=transport),
        session_factory,
        recovery=Recovery(session_factory),
        clock=lambda: NOW,
    )
    with pytest.raises(StreamSupervisorError):
        asyncio.run(supervisor.run(lambda decoded: asyncio.sleep(0)))
    assert len(calls) == 1
    assert calls[0][0].ingest_transport == transport
