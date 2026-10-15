"""Offline acceptance tests for explicit raw-log intake and transaction hydration."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import traceback
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event, select
from websockets.exceptions import ConnectionClosed
from websockets.frames import Close

from app.config import Settings
from app.db.models import provider_catalog_snapshots, raw_events
from app.db.session import create_all, create_database_engine, create_session_factory
from app.db.stream_cursors import StreamCursorRepository
from app.decoders import DecodeResult, DecoderInput
from app.domain.enums import DecodeStatus
from app.domain.models import DecodedEvent
from app.integrations import logs_subscribe as logs_module
from app.integrations.logs_subscribe import LogsSubscribeClient, LogsSubscribeError
from app.integrations.parsed_streams import ParsedStreamsError

API_KEY = "synthetic-logs-subscribe-test-secret"
SIGNATURE = "1" * 64
SECOND_SIGNATURE = "1" * 63 + "2"
SLOT = 300_000_001
OBSERVED_AT = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def run(coroutine):
    return asyncio.run(coroutine)


def live_settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        **{
            "app_env": "test",
            "ingest_mode": "live",
            "ingest_transport": "logs_subscribe",
            "helius_api_key": API_KEY,
            **overrides,
        },
    )


@pytest.fixture
def session_factory(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'logs.db'}")
    create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def stored_rows(session_factory, table=raw_events):
    with session_factory() as observer:
        return list(observer.execute(select(table).order_by(table.c.id)).mappings())


def notification(*, subscription=101, slot=SLOT, signature=SIGNATURE, error=None):
    return {
        "jsonrpc": "2.0",
        "method": "logsNotification",
        "params": {
            "subscription": subscription,
            "result": {
                "context": {"slot": slot},
                "value": {
                    "signature": signature,
                    "err": error,
                    "logs": ["Program log: Instruction: Create", "Program success"],
                },
            },
        },
    }


def transaction_result(*, slot=SLOT, signature=SIGNATURE):
    # The spy decoder needs no binary event fixture: this represents a full RPC body.
    return {
        "slot": slot,
        "blockTime": None,
        "meta": {
            "err": None,
            "fee": 5000,
            "preBalances": [10000],
            "postBalances": [5000],
            "innerInstructions": [],
            "logMessages": ["Program log: Instruction: Create", "Program success"],
            "preTokenBalances": [],
            "postTokenBalances": [],
            "loadedAddresses": {"writable": [], "readonly": []},
        },
        "transaction": {
            "signatures": [signature],
            "message": {
                "accountKeys": ["1" * 32],
                "header": {
                    "numRequiredSignatures": 1,
                    "numReadonlySignedAccounts": 0,
                    "numReadonlyUnsignedAccounts": 0,
                },
                "recentBlockhash": "1" * 32,
                "instructions": [],
            },
        },
        "version": "legacy",
    }


class Pause:
    """A cancellable wait that exercises a socket timeout without real networking."""


class FakeSocket:
    def __init__(self):
        self.sent: list[dict[str, Any]] = []
        self.incoming: deque[str | bytes | Exception | Pause] = deque()
        self.response_factory = lambda request: {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": request["id"] + 100,
        }
        self.before_response = None
        self.send_error: Exception | None = None
        self.send_pause = False
        self.ping_error: Exception | None = None
        self.close_error: Exception | None = None
        self.closed = 0
        self.pings = 0

    async def send(self, message: str) -> None:
        if self.send_error is not None:
            raise self.send_error
        if self.send_pause:
            await asyncio.sleep(60)
        request = json.loads(message)
        self.sent.append(request)
        if self.before_response is not None:
            self.before_response(request, self)
        if self.response_factory is not None:
            response = self.response_factory(request)
            if response is not None:
                self.incoming.append(json.dumps(response))

    async def recv(self) -> str | bytes:
        if not self.incoming:
            raise RuntimeError(f"unexpected mock receive {API_KEY}")
        item = self.incoming.popleft()
        if isinstance(item, Exception):
            raise item
        if isinstance(item, Pause):
            await asyncio.sleep(60)
            raise AssertionError("mock wait must be cancelled by the receive timeout")
        return item

    async def ping(self) -> None:
        self.pings += 1
        if self.ping_error is not None:
            raise self.ping_error

    async def close(self) -> None:
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


class FakeRpc:
    def __init__(self, session_factory):
        self.session_factory = session_factory
        self.responses = deque([{"result": transaction_result()}])
        self.calls: list[tuple[str, list[object]]] = []
        self.observed_journals: list[list[Any]] = []
        self.closed = 0
        self.pause = False
        self.cancelled = False
        self.close_error: Exception | None = None

    async def rpc(self, method: str, params: list[object]) -> dict[str, Any]:
        self.calls.append((method, copy.deepcopy(params)))
        self.observed_journals.append(stored_rows(self.session_factory))
        # Independent SQL sessions prove the notification committed before HTTP starts.
        assert self.observed_journals[-1]
        if self.pause:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        response = self.responses[0] if len(self.responses) == 1 else self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)

    async def close(self) -> None:
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


class CapturingDecoder:
    def __init__(self, session_factory, settings):
        self.session_factory = session_factory
        self.settings = settings
        self.inputs: list[DecoderInput] = []
        self.observed_journals: list[list[Any]] = []
        self.status = DecodeStatus.DECODED
        self.error: Exception | None = None
        self.wrong_raw_id = False

    def decode(self, event: DecoderInput) -> DecodeResult:
        rows = stored_rows(self.session_factory)
        self.observed_journals.append(rows)
        persisted = next(row for row in rows if row["id"] == event.raw_event_id)
        assert persisted["payload_json"] == event.payload_json
        assert len(rows) >= 2
        self.inputs.append(event)
        if self.error is not None:
            raise self.error
        raw_id = event.raw_event_id + 100 if self.wrong_raw_id else event.raw_event_id
        if self.status == DecodeStatus.DECODED:
            return DecodeResult(
                raw_event_id=raw_id,
                status=self.status,
                events=[
                    DecodedEvent(
                        event_name="CreateEvent",
                        program_id=self.settings.pump_program_id,
                        event_index=0,
                        fields={"test": True},
                        decode_status=DecodeStatus.DECODED,
                    )
                ],
            )
        return DecodeResult(
            raw_event_id=raw_id,
            status=self.status,
            safe_error_code="test_unclassified",
            safe_error_message="mock decoder retains raw evidence",
        )


class Harness:
    def __init__(self, session_factory, *, settings=None, decoder=True, **client_options):
        self.settings = settings or live_settings()
        self.socket = FakeSocket()
        self.rpc = FakeRpc(session_factory)
        self.decoder = CapturingDecoder(session_factory, self.settings)
        self.connections: list[tuple[str, dict[str, str]]] = []
        self.connect_error: Exception | None = None
        self.now = OBSERVED_AT

        async def connector(url, headers):
            self.connections.append((url, dict(headers)))
            if self.connect_error is not None:
                raise self.connect_error
            return self.socket

        self.client = LogsSubscribeClient(
            self.settings,
            session_factory,
            rpc=self.rpc,
            connector=connector,
            clock=lambda: self.now,
            **({"decoder": self.decoder} if decoder else {}),
            **client_options,
        )

    async def receive(self, payload=None):
        self.socket.incoming.append(json.dumps(notification() if payload is None else payload))
        return await self.client.receive_once()


def assert_safe_error(error: LogsSubscribeError) -> None:
    assert error.code
    assert API_KEY not in str(error)
    assert API_KEY not in repr(error)
    assert API_KEY not in "".join(traceback.format_exception(error))


def assert_raw_only(result: DecodeResult) -> None:
    assert result.status == DecodeStatus.RAW_ONLY
    assert result.events == []
    assert result.instructions == []
    assert result.safe_error_code
    assert result.safe_error_message
    assert API_KEY not in result.model_dump_json()


def test_start_subscribes_to_both_programs_without_discovery(session_factory):
    harness = Harness(session_factory)

    async def scenario():
        await harness.client.start()
        assert harness.client.heartbeat.state == "subscribed"
        assert harness.client.heartbeat.last_contact_at == OBSERVED_AT
        await harness.client.close()

    run(scenario())
    assert harness.connections == [(harness.settings.helius_logs_ws_url, {"x-api-key": API_KEY})]
    assert harness.socket.sent == [
        {
            "jsonrpc": "2.0",
            "id": index,
            "method": "logsSubscribe",
            "params": [{"mentions": [program]}, {"commitment": "confirmed"}],
        }
        for index, program in enumerate(
            [harness.settings.pump_program_id, harness.settings.pump_amm_program_id], start=1
        )
    ]
    assert stored_rows(session_factory, provider_catalog_snapshots) == []
    assert harness.rpc.calls == []
    assert harness.socket.closed == 1
    assert harness.rpc.closed == 0


@pytest.mark.parametrize(
    "settings",
    [
        Settings(_env_file=None),
        live_settings(ingest_transport="parsed_streams"),
        live_settings(ingest_transport="laserstream"),
        live_settings(solana_network="devnet"),
        live_settings(solana_network="testnet"),
    ],
)
def test_only_explicit_mainnet_live_logs_mode_connects(session_factory, settings):
    harness = Harness(session_factory, settings=settings)
    with pytest.raises(LogsSubscribeError) as raised:
        run(harness.client.start())
    assert_safe_error(raised.value)
    assert harness.connections == []
    assert harness.rpc.calls == []


def test_durable_notification_precedes_fetch_and_durable_transaction_precedes_decode(
    session_factory,
):
    harness = Harness(session_factory)
    payload = notification()
    body = transaction_result()

    async def scenario():
        await harness.client.start()
        result = await harness.receive(payload)
        assert result.status == DecodeStatus.DECODED
        assert result.raw_event_id == stored_rows(session_factory)[1]["id"]
        assert harness.client.heartbeat.state == "receiving"
        assert harness.client.heartbeat.last_persisted_slot == SLOT
        assert harness.client.heartbeat.last_signature == SIGNATURE
        assert harness.client.heartbeat.notifications_received == 1
        await harness.client.close()

    run(scenario())
    assert harness.rpc.calls == [
        (
            "getTransaction",
            [
                SIGNATURE,
                {
                    "commitment": "confirmed",
                    "encoding": "json",
                    "maxSupportedTransactionVersion": 1,
                },
            ],
        )
    ]
    rows = stored_rows(session_factory)
    assert len(rows) == 2
    assert len(harness.rpc.observed_journals[0]) == 1
    assert len(harness.decoder.observed_journals[0]) == 2
    assert json.loads(rows[0]["payload_json"]) == payload
    assert json.loads(rows[1]["payload_json"]) == body
    assert json.loads(harness.decoder.inputs[0].payload_json) == body
    assert rows[0]["status"] == "decoded"
    assert rows[1]["status"] == "decoded"
    for row in rows:
        assert row["transport"] == "logs_subscribe"
        assert row["signature"] == SIGNATURE
        assert row["slot"] == SLOT
        assert row["commitment"] == "confirmed"
        assert row["idl_revision"]
        assert row["payload_sha256"] == hashlib.sha256(row["payload_json"].encode()).hexdigest()


@pytest.mark.parametrize("emission", ["supported", "text_only", "unknown_discriminator"])
def test_notification_fetch_journal_and_default_binary_decoder_end_to_end(
    session_factory, emission
):
    harness = Harness(session_factory, decoder=False)
    program = harness.settings.pump_program_id
    body = transaction_result()
    message = body["transaction"]["message"]
    message["accountKeys"].append(program)
    message["instructions"] = [{"programIdIndex": 1, "accounts": [], "data": ""}]
    # Pinned CompleteEvent: discriminator, three pubkeys, i64 time, quote pubkey.
    discriminator = bytes([95, 114, 97, 156, 212, 46, 152, 8])
    if emission == "unknown_discriminator":
        discriminator = b"unknown!"
    binary = discriminator + bytes(96) + (1791853200).to_bytes(8, "little", signed=True) + bytes(32)
    body["meta"]["logMessages"] = [
        f"Program {program} invoke [1]",
        "Program log: Instruction: Create"
        if emission == "text_only"
        else "Program data: " + base64.b64encode(binary).decode(),
        f"Program {program} success",
    ]
    harness.rpc.responses = deque([{"result": body}])

    async def scenario():
        await harness.client.start()
        result = await harness.receive()
        rows = stored_rows(session_factory)
        assert len(rows) == 2
        assert result.raw_event_id == rows[1]["id"]
        assert json.loads(rows[1]["payload_json"]) == body
        if emission == "supported":
            assert result.status == DecodeStatus.DECODED
            assert [row["status"] for row in rows] == ["decoded", "decoded"]
            assert len(result.events) == 1
            decoded = result.events[0]
            assert decoded.event_name == "CompleteEvent"
            assert decoded.fields["timestamp"] == 1791853200
            assert decoded.fields["mint"] == "1" * 32
            assert decoded.event_index == decoded.source_instruction_index == 0
        else:
            assert_raw_only(result)
            assert [row["status"] for row in rows] == ["raw_only", "raw_only"]
        await harness.client.close()

    run(scenario())
    assert len(harness.rpc.observed_journals[0]) == 1


@pytest.mark.parametrize("subscription", [101, 102])
def test_each_confirmed_program_subscription_can_hydrate(session_factory, subscription):
    harness = Harness(session_factory)

    async def scenario():
        await harness.client.start()
        result = await harness.receive(notification(subscription=subscription))
        assert result.status == DecodeStatus.DECODED
        await harness.client.close()

    run(scenario())
    assert len(harness.rpc.calls) == 1


@pytest.mark.parametrize("location", ["notification", "params", "result", "context", "value"])
def test_unknown_notification_fields_remain_raw_and_never_fetch(session_factory, location):
    harness = Harness(session_factory)
    payload = notification()
    objects = {
        "notification": payload,
        "params": payload["params"],
        "result": payload["params"]["result"],
        "context": payload["params"]["result"]["context"],
        "value": payload["params"]["result"]["value"],
    }
    objects[location]["future_provider_field"] = {"value": 1}

    async def scenario():
        await harness.client.start()
        assert_raw_only(await harness.receive(payload))
        await harness.client.close()

    run(scenario())
    rows = stored_rows(session_factory)
    assert len(rows) == 1
    assert json.loads(rows[0]["payload_json"]) == payload
    assert rows[0]["status"] == "raw_only"
    assert harness.rpc.calls == []
    assert harness.decoder.inputs == []


def test_failed_notification_is_retained_without_fetch_or_log_text_classification(session_factory):
    harness = Harness(session_factory)
    payload = notification(error={"InstructionError": [0, {"Custom": 1}]})

    async def scenario():
        await harness.client.start()
        assert_raw_only(await harness.receive(payload))
        await harness.client.close()

    run(scenario())
    assert len(stored_rows(session_factory)) == 1
    assert stored_rows(session_factory)[0]["status"] == "raw_only"
    assert harness.rpc.calls == []
    assert harness.decoder.inputs == []


@pytest.mark.parametrize(
    "response",
    [
        {"result": None},
        {"error": {"code": -32000, "message": API_KEY}},
        RuntimeError(API_KEY),
        {"result": []},
        {},
    ],
)
def test_unavailable_lookup_retains_notification_with_safe_replayable_outcome(
    session_factory, response
):
    harness = Harness(session_factory)
    harness.rpc.responses = deque([response])

    async def scenario():
        await harness.client.start()
        result = await harness.receive()
        assert_raw_only(result)
        row = stored_rows(session_factory)[0]
        assert result.raw_event_id == row["id"]
        assert row["status"] == "raw_only"
        assert row["safe_error_code"]
        assert API_KEY not in row["safe_error_message"]
        assert harness.client.heartbeat.state == "receiving"
        await harness.client.close()

    run(scenario())
    assert len(stored_rows(session_factory)) == 1
    assert harness.decoder.inputs == []


def test_repeated_notification_can_recover_after_null_lookup_without_duplicate_journal_rows(
    session_factory,
):
    harness = Harness(session_factory)
    harness.rpc.responses = deque([{"result": None}, {"result": transaction_result()}])

    async def scenario():
        await harness.client.start()
        first = await harness.receive()
        assert_raw_only(first)
        second = await harness.receive()
        assert second.status == DecodeStatus.DECODED
        assert second.raw_event_id != first.raw_event_id
        assert len(stored_rows(session_factory)) == 2
        assert harness.client.heartbeat.notifications_received == 2
        await harness.client.close()

    run(scenario())
    assert len(harness.rpc.calls) == 2
    assert len(harness.decoder.inputs) == 1


def test_successful_duplicates_share_both_immutable_journal_identities(session_factory):
    harness = Harness(session_factory)

    async def scenario():
        await harness.client.start()
        first = await harness.receive()
        second = await harness.receive()
        assert first.raw_event_id == second.raw_event_id
        assert first.status == second.status == DecodeStatus.DECODED
        assert len(stored_rows(session_factory)) == 2
        assert harness.client.heartbeat.notifications_received == 2
        await harness.client.close()

    run(scenario())


@pytest.mark.parametrize("mismatch", ["slot", "signature", "missing_slot", "missing_signatures"])
def test_lookup_identity_mismatch_preserves_body_without_calling_decoder(session_factory, mismatch):
    harness = Harness(session_factory)
    body = transaction_result()
    if mismatch == "slot":
        body["slot"] += 1
    elif mismatch == "signature":
        body["transaction"]["signatures"][0] = SECOND_SIGNATURE
    elif mismatch == "missing_slot":
        del body["slot"]
    else:
        del body["transaction"]["signatures"]
    harness.rpc.responses = deque([{"result": body}])

    async def scenario():
        await harness.client.start()
        result = await harness.receive()
        assert_raw_only(result)
        rows = stored_rows(session_factory)
        assert len(rows) == 2
        assert result.raw_event_id == rows[1]["id"]
        assert json.loads(rows[1]["payload_json"]) == body
        # Attempt association uses the requested identity even when provider data differs.
        assert rows[1]["signature"] == SIGNATURE
        assert rows[1]["slot"] == SLOT
        assert rows[1]["status"] == "raw_only"
        await harness.client.close()

    run(scenario())
    assert harness.decoder.inputs == []


@pytest.mark.parametrize("body_shape", ["failed_meta", "missing_meta", "null_meta"])
def test_failed_or_missing_transaction_meta_is_retained_without_calling_decoder(
    session_factory, body_shape
):
    harness = Harness(session_factory)
    body = transaction_result()
    if body_shape == "failed_meta":
        body["meta"]["err"] = {"InstructionError": [0, {"Custom": 1}]}
    elif body_shape == "missing_meta":
        del body["meta"]
    else:
        body["meta"] = None
    harness.rpc.responses = deque([{"result": body}])

    async def scenario():
        await harness.client.start()
        result = await harness.receive()
        assert_raw_only(result)
        rows = stored_rows(session_factory)
        assert len(rows) == 2
        assert result.raw_event_id == rows[1]["id"]
        assert json.loads(rows[1]["payload_json"]) == body
        assert all(row["status"] == "raw_only" for row in rows)
        await harness.client.close()

    run(scenario())
    assert harness.decoder.inputs == []


@pytest.mark.parametrize("failure", ["raised_exception", "wrong_raw_id", "unknown_result"])
def test_decoder_failure_is_safe_raw_only_and_preserves_both_durable_payloads(
    session_factory, failure
):
    harness = Harness(session_factory)
    if failure == "raised_exception":
        harness.decoder.error = RuntimeError(API_KEY)
    elif failure == "wrong_raw_id":
        harness.decoder.wrong_raw_id = True
    else:
        harness.decoder.status = DecodeStatus.RAW_ONLY

    async def scenario():
        await harness.client.start()
        result = await harness.receive()
        assert_raw_only(result)
        rows = stored_rows(session_factory)
        assert len(rows) == 2
        assert result.raw_event_id == rows[1]["id"]
        assert all(row["status"] == "raw_only" for row in rows)
        await harness.client.close()

    run(scenario())


def test_failed_transaction_decode_keeps_valid_logs_trigger_raw_only(session_factory):
    harness = Harness(session_factory)
    harness.decoder.status = DecodeStatus.FAILED

    async def scenario():
        await harness.client.start()
        result = await harness.receive()
        assert result.status == DecodeStatus.FAILED
        assert result.events == []
        assert result.instructions == []
        rows = stored_rows(session_factory)
        assert len(rows) == 2
        assert result.raw_event_id == rows[1]["id"]
        assert rows[0]["status"] == "raw_only"
        assert rows[0]["safe_error_code"]
        assert rows[0]["safe_error_message"]
        assert rows[1]["status"] == "failed"
        assert rows[1]["safe_error_code"] == "test_unclassified"
        assert json.loads(rows[0]["payload_json"]) == notification()
        assert json.loads(rows[1]["payload_json"]) == transaction_result()
        assert API_KEY not in result.model_dump_json()
        await harness.client.close()

    run(scenario())


def test_late_notifications_do_not_rewind_durable_heartbeat(session_factory):
    harness = Harness(session_factory)
    harness.rpc.responses = deque(
        [
            {"result": transaction_result()},
            {"result": transaction_result(slot=SLOT - 1, signature=SECOND_SIGNATURE)},
        ]
    )

    async def scenario():
        await harness.client.start()
        await harness.receive()
        harness.now += timedelta(seconds=5)
        await harness.receive(notification(slot=SLOT - 1, signature=SECOND_SIGNATURE))
        assert harness.client.heartbeat.last_persisted_slot == SLOT
        assert harness.client.heartbeat.last_signature == SIGNATURE
        assert harness.client.heartbeat.last_notification_at == harness.now
        assert harness.client.heartbeat.notifications_received == 2
        await harness.client.close()

    run(scenario())


def test_notification_during_second_subscription_is_durable_and_queued(session_factory):
    harness = Harness(session_factory)

    def before_response(request, socket):
        if request["id"] == 2:
            socket.incoming.append(json.dumps(notification()))

    harness.socket.before_response = before_response

    async def scenario():
        await harness.client.start()
        assert len(stored_rows(session_factory)) == 2
        result = await harness.client.receive_once()
        assert result.status == DecodeStatus.DECODED
        assert harness.socket.incoming == deque()
        assert harness.client.heartbeat.notifications_received == 1
        await harness.client.close()

    run(scenario())
    assert len(harness.rpc.calls) == 1


def test_close_discards_queued_handshake_results_and_does_not_close_injected_rpc(session_factory):
    harness = Harness(session_factory)

    def before_response(request, socket):
        if request["id"] == 2:
            socket.incoming.append(json.dumps(notification()))

    harness.socket.before_response = before_response

    async def scenario():
        await harness.client.start()
        await harness.client.close()
        await harness.client.close()
        with pytest.raises(LogsSubscribeError) as raised:
            await harness.client.receive_once()
        assert_safe_error(raised.value)
        assert harness.client.heartbeat.state == "disconnected"

    run(scenario())
    assert harness.socket.closed == 1
    assert harness.rpc.closed == 0


def test_handshake_notification_queue_is_bounded_and_closes_safely(session_factory):
    harness = Harness(session_factory)

    def before_response(request, socket):
        if request["id"] == 2:
            for _ in range(65):
                socket.incoming.append(json.dumps(notification()))

    harness.socket.before_response = before_response
    with pytest.raises(LogsSubscribeError) as raised:
        run(harness.client.start())
    assert_safe_error(raised.value)
    assert harness.client.heartbeat.state == "error"
    assert harness.socket.closed == 1
    # Repeated provider messages cannot grow the durable journal without bound either.
    assert len(stored_rows(session_factory)) == 2


def test_start_cancellation_closes_partially_subscribed_socket(session_factory):
    harness = Harness(session_factory)
    harness.socket.response_factory = lambda request: None
    harness.socket.incoming.append(Pause())

    async def scenario():
        task = asyncio.create_task(harness.client.start())
        for _ in range(100):
            if harness.socket.sent:
                break
            await asyncio.sleep(0)
        assert harness.socket.sent
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert harness.socket.closed == 1
    assert harness.rpc.closed == 0
    assert harness.rpc.calls == []


@pytest.mark.parametrize("close_error", [None, RuntimeError(API_KEY)])
def test_owned_http_reader_is_closed_when_client_shuts_down(
    session_factory, monkeypatch, close_error
):
    owned = FakeRpc(session_factory)
    owned.close_error = close_error
    constructors = []

    def rpc_factory(*args, **kwargs):
        constructors.append((args, kwargs))
        return owned

    monkeypatch.setattr(logs_module, "HeliusHttpClient", rpc_factory)
    socket = FakeSocket()

    async def connector(url, headers):
        return socket

    client = LogsSubscribeClient(
        live_settings(),
        session_factory,
        connector=connector,
        decoder=CapturingDecoder(session_factory, live_settings()),
    )

    async def scenario():
        await client.start()
        socket.incoming.append(json.dumps(notification()))
        assert (await client.receive_once()).status == DecodeStatus.DECODED
        await client.close()
        await client.close()
        assert client.heartbeat.state == "disconnected"

    run(scenario())
    assert len(constructors) == 1
    assert owned.closed == 1


@pytest.mark.parametrize("subscription", [-1, True, "101", None, 1.0])
def test_subscription_result_requires_nonnegative_integer(session_factory, subscription):
    harness = Harness(session_factory)
    harness.socket.response_factory = lambda request: {
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": subscription,
    }
    with pytest.raises(LogsSubscribeError) as raised:
        run(harness.client.start())
    assert_safe_error(raised.value)
    assert harness.socket.closed == 1
    assert harness.rpc.calls == []


def test_duplicate_subscription_ids_are_rejected(session_factory):
    harness = Harness(session_factory)
    harness.socket.response_factory = lambda request: {
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": 101,
    }
    with pytest.raises(LogsSubscribeError) as raised:
        run(harness.client.start())
    assert_safe_error(raised.value)
    assert harness.socket.closed == 1


@pytest.mark.parametrize("response_id", [True, "1", 999])
def test_subscription_response_matches_request_integer(session_factory, response_id):
    harness = Harness(session_factory)
    harness.socket.response_factory = lambda request: {
        "jsonrpc": "2.0",
        "id": response_id,
        "result": 101,
    }
    with pytest.raises(LogsSubscribeError) as raised:
        run(harness.client.start())
    assert_safe_error(raised.value)
    assert harness.socket.closed == 1


def test_subscription_rpc_error_is_safe_and_never_retried_or_switched_transport(session_factory):
    harness = Harness(session_factory)
    harness.socket.response_factory = lambda request: {
        "jsonrpc": "2.0",
        "id": request["id"],
        "error": {"code": -32000, "message": API_KEY},
    }
    with pytest.raises(LogsSubscribeError) as raised:
        run(harness.client.start())
    assert_safe_error(raised.value)
    assert len(harness.connections) == 1
    assert len(harness.socket.sent) == 1
    assert harness.rpc.calls == []


@pytest.mark.parametrize("failure", ["connect", "send"])
def test_transport_exception_is_redacted_without_automatic_fallback(session_factory, failure):
    harness = Harness(session_factory)
    if failure == "connect":
        harness.connect_error = RuntimeError(API_KEY)
    else:
        harness.socket.send_error = RuntimeError(API_KEY)
    with pytest.raises(LogsSubscribeError) as raised:
        run(harness.client.start())
    assert_safe_error(raised.value)
    assert len(harness.connections) == 1
    assert harness.rpc.calls == []


@pytest.mark.parametrize("retryable", [False, True])
@pytest.mark.parametrize("stage", ["connect", "receive"])
def test_safe_connector_retry_classification_survives_transport_boundary(
    session_factory, retryable, stage
):
    harness = Harness(session_factory)
    error = ParsedStreamsError("stream_connection_failed", retryable=retryable)

    async def scenario():
        if stage == "connect":
            harness.connect_error = error
            operation = harness.client.start()
        else:
            await harness.client.start()
            harness.socket.incoming.append(error)
            operation = harness.client.receive_once()
        with pytest.raises(LogsSubscribeError) as raised:
            await operation
        assert_safe_error(raised.value)
        assert raised.value.retryable is retryable
        assert harness.client.heartbeat.state == "error"

    run(scenario())
    assert len(harness.connections) == 1
    assert harness.rpc.calls == []


@pytest.mark.parametrize("blocked_stage", ["send", "receive"])
def test_handshake_timeout_closes_socket_and_has_safe_diagnostic(session_factory, blocked_stage):
    harness = Harness(session_factory, handshake_timeout=0.01)
    if blocked_stage == "send":
        harness.socket.send_pause = True
    else:
        harness.socket.response_factory = lambda request: None
        harness.socket.incoming.append(Pause())
    with pytest.raises(LogsSubscribeError) as raised:
        run(harness.client.start())
    assert_safe_error(raised.value)
    assert harness.socket.closed == 1
    assert harness.client.heartbeat.state == "error"


def test_receive_timeout_checks_pong_and_continues_without_rpc_heartbeat(session_factory):
    harness = Harness(session_factory, receive_timeout=0.01)

    async def scenario():
        await harness.client.start()
        harness.socket.incoming.append(Pause())
        harness.socket.incoming.append(json.dumps(notification()))
        result = await harness.client.receive_once()
        assert result.status == DecodeStatus.DECODED
        assert harness.socket.pings == 1
        await harness.client.close()

    run(scenario())
    assert [method for method, params in harness.rpc.calls] == ["getTransaction"]


def test_failed_pong_closes_socket_without_fabricating_last_contact(session_factory):
    harness = Harness(session_factory, receive_timeout=0.01)

    async def scenario():
        await harness.client.start()
        previous_contact = harness.client.heartbeat.last_contact_at
        harness.now += timedelta(seconds=60)
        harness.socket.incoming.append(Pause())
        harness.socket.ping_error = RuntimeError(API_KEY)
        with pytest.raises(LogsSubscribeError) as raised:
            await harness.client.receive_once()
        assert_safe_error(raised.value)
        assert harness.client.heartbeat.state == "error"
        assert harness.client.heartbeat.last_contact_at == previous_contact

    run(scenario())
    assert harness.socket.closed == 1
    assert harness.rpc.calls == []


@pytest.mark.parametrize(
    "message",
    ["not-json", "[]", '{"jsonrpc":"2.0","params":NaN}', b"\xff"],
)
def test_invalid_notification_json_closes_safely_without_fetch(session_factory, message):
    harness = Harness(session_factory)

    async def scenario():
        await harness.client.start()
        harness.socket.incoming.append(message)
        with pytest.raises(LogsSubscribeError) as raised:
            await harness.client.receive_once()
        assert_safe_error(raised.value)

    run(scenario())
    assert stored_rows(session_factory) == []
    assert harness.rpc.calls == []
    assert harness.decoder.inputs == []
    assert harness.socket.closed == 1


def test_duplicate_json_members_are_rejected_without_silent_collapse(session_factory):
    harness = Harness(session_factory)
    serialized = json.dumps(notification()).replace(
        '"signature": "' + SIGNATURE + '"',
        '"signature": "' + SIGNATURE + '", "signature": "' + SIGNATURE + '"',
    )

    async def scenario():
        await harness.client.start()
        harness.socket.incoming.append(serialized)
        with pytest.raises(LogsSubscribeError) as raised:
            await harness.client.receive_once()
        assert_safe_error(raised.value)

    run(scenario())
    assert stored_rows(session_factory) == []
    assert harness.rpc.calls == []


def test_unknown_subscription_is_rejected_before_any_fetch(session_factory):
    harness = Harness(session_factory)

    async def scenario():
        await harness.client.start()
        with pytest.raises(LogsSubscribeError) as raised:
            await harness.receive(notification(subscription=999))
        assert_safe_error(raised.value)

    run(scenario())
    assert harness.rpc.calls == []
    assert harness.decoder.inputs == []


def test_cancellation_during_lookup_keeps_committed_notification_for_replay(session_factory):
    harness = Harness(session_factory)
    harness.rpc.pause = True

    async def scenario():
        await harness.client.start()
        task = asyncio.create_task(harness.receive())
        for _ in range(100):
            if harness.rpc.calls:
                break
            await asyncio.sleep(0)
        assert harness.rpc.calls
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(stored_rows(session_factory)) == 1
        assert harness.decoder.inputs == []
        await harness.client.close()

    run(scenario())
    assert json.loads(stored_rows(session_factory)[0]["payload_json"]) == notification()


def test_stalled_lookup_is_bounded_and_leaves_durable_replayable_notification(session_factory):
    harness = Harness(session_factory, fetch_timeout=0.01)
    harness.rpc.pause = True

    async def scenario():
        await harness.client.start()
        result = await asyncio.wait_for(harness.receive(), timeout=1)
        assert_raw_only(result)
        rows = stored_rows(session_factory)
        assert len(rows) == 1
        assert result.raw_event_id == rows[0]["id"]
        assert rows[0]["status"] == "raw_only"
        assert rows[0]["safe_error_code"] == "transaction_fetch_failed"
        assert harness.client.heartbeat.state == "receiving"
        assert harness.client.heartbeat.last_persisted_slot == SLOT
        assert harness.rpc.cancelled
        assert harness.decoder.inputs == []
        await harness.client.close()

    run(scenario())
    assert json.loads(stored_rows(session_factory)[0]["payload_json"]) == notification()


def test_journal_failure_prevents_http_fetch_and_durable_heartbeat(session_factory):
    harness = Harness(session_factory)

    async def scenario():
        await harness.client.start()
        with session_factory() as session:
            raw_events.drop(session.connection())
            session.commit()
        with pytest.raises(LogsSubscribeError) as raised:
            await harness.receive()
        assert_safe_error(raised.value)
        assert harness.client.heartbeat.last_persisted_slot is None
        assert harness.client.heartbeat.notifications_received == 0

    run(scenario())
    assert harness.rpc.calls == []
    assert harness.decoder.inputs == []
    assert harness.socket.closed == 1


def test_transaction_journal_sql_failure_preserves_committed_notification(session_factory):
    harness = Harness(session_factory)
    engine = session_factory.kw["bind"]
    inserts = 0

    def reject_transaction_insert(connection, cursor, statement, parameters, context, executemany):
        nonlocal inserts
        if statement.startswith("INSERT INTO raw_events"):
            inserts += 1
            if inserts == 2:
                raise RuntimeError(API_KEY)

    event.listen(engine, "before_cursor_execute", reject_transaction_insert)

    async def scenario():
        await harness.client.start()
        with pytest.raises(LogsSubscribeError) as raised:
            await harness.receive()
        assert_safe_error(raised.value)
        assert harness.client.heartbeat.last_persisted_slot == SLOT
        assert harness.client.heartbeat.notifications_received == 1

    try:
        run(scenario())
    finally:
        event.remove(engine, "before_cursor_execute", reject_transaction_insert)
    rows = stored_rows(session_factory)
    assert len(rows) == 1
    assert json.loads(rows[0]["payload_json"]) == notification()
    assert harness.decoder.inputs == []
    assert harness.socket.closed == 1


def test_close_is_idempotent_when_socket_close_raises(session_factory):
    harness = Harness(session_factory)

    async def scenario():
        await harness.client.start()
        harness.socket.close_error = RuntimeError(API_KEY)
        await harness.client.close()
        await harness.client.close()
        assert harness.client.heartbeat.state == "disconnected"

    run(scenario())
    assert harness.socket.closed == 1
    assert harness.rpc.closed == 0


def test_second_start_requires_explicit_close(session_factory):
    harness = Harness(session_factory)

    async def scenario():
        await harness.client.start()
        with pytest.raises(LogsSubscribeError) as raised:
            await harness.client.start()
        assert_safe_error(raised.value)
        assert harness.client.heartbeat.state == "subscribed"
        assert len(harness.connections) == 1
        await harness.client.close()

    run(scenario())


@pytest.mark.parametrize("field", ["handshake_timeout", "receive_timeout", "fetch_timeout"])
@pytest.mark.parametrize("value", [0, -1, True, float("nan"), float("inf"), "10"])
def test_invalid_timeout_is_rejected_without_opening_a_socket(session_factory, field, value):
    with pytest.raises(LogsSubscribeError) as raised:
        Harness(session_factory, **{field: value})
    assert_safe_error(raised.value)


def test_notification_cursors_are_per_program_and_never_point_to_fetched_body(session_factory):
    harness = Harness(session_factory)
    harness.rpc.responses = deque(
        [
            {"result": transaction_result(slot=SLOT + 10)},
            {"result": transaction_result(slot=SLOT + 5)},
            {"result": transaction_result(slot=SLOT)},
        ]
    )

    async def scenario():
        await harness.client.start()
        for subscription, slot in [(101, SLOT + 10), (102, SLOT + 5), (101, SLOT)]:
            await harness.receive(notification(subscription=subscription, slot=slot))
        repository = StreamCursorRepository(session_factory)
        pump = repository.load("logs_subscribe", harness.settings.pump_program_id)
        amm = repository.load("logs_subscribe", harness.settings.pump_amm_program_id)
        assert pump is not None and amm is not None
        assert pump.slot == SLOT + 10
        assert amm.slot == SLOT + 5
        rows = stored_rows(session_factory)
        assert pump.raw_event_id == rows[0]["id"]
        assert amm.raw_event_id == rows[2]["id"]
        assert pump.raw_event_id != rows[1]["id"]
        assert amm.raw_event_id != rows[3]["id"]
        assert pump.signature == amm.signature == SIGNATURE
        assert pump.received_at == amm.received_at == OBSERVED_AT
        await harness.client.close()

    run(scenario())


def test_interleaved_handshake_notification_commits_cursor_before_fetch(session_factory):
    harness = Harness(session_factory)

    def before_response(request, socket):
        if request["id"] == 2:
            socket.incoming.append(json.dumps(notification(error="InstructionError")))

    harness.socket.before_response = before_response

    async def scenario():
        await harness.client.start()
        repository = StreamCursorRepository(session_factory)
        pump = repository.load("logs_subscribe", harness.settings.pump_program_id)
        assert pump is not None
        assert pump.raw_event_id == stored_rows(session_factory)[0]["id"]
        assert repository.load("logs_subscribe", harness.settings.pump_amm_program_id) is None
        result = await harness.client.receive_once()
        assert_raw_only(result)
        assert result.raw_event_id == pump.raw_event_id
        assert harness.rpc.calls == []
        await harness.client.close()

    run(scenario())


def test_cursor_insert_failure_rolls_back_notification_and_prevents_lookup(session_factory):
    harness = Harness(session_factory)
    engine = session_factory.kw["bind"]

    def reject_cursor(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO cursors"):
            raise RuntimeError(API_KEY)

    async def scenario():
        await harness.client.start()
        with pytest.raises(LogsSubscribeError) as raised:
            await harness.receive()
        assert_safe_error(raised.value)
        assert raised.value.retryable is False
        assert harness.client.heartbeat.notifications_received == 0

    event.listen(engine, "before_cursor_execute", reject_cursor)
    try:
        run(scenario())
    finally:
        event.remove(engine, "before_cursor_execute", reject_cursor)
    assert stored_rows(session_factory) == []
    assert harness.rpc.calls == []
    assert harness.decoder.inputs == []
    assert (
        StreamCursorRepository(session_factory).load(
            "logs_subscribe", harness.settings.pump_program_id
        )
        is None
    )


@pytest.mark.parametrize(
    "code,retryable",
    [
        (1000, True),
        (1001, True),
        (1005, True),
        (1006, True),
        (1002, False),
        (1003, False),
        (1007, False),
        (1008, False),
        (1009, False),
    ],
)
def test_socket_close_codes_keep_retry_policy_without_automatic_fallback(
    session_factory, code, retryable
):
    harness = Harness(session_factory)

    async def scenario():
        await harness.client.start()
        harness.socket.incoming.append(ConnectionClosed(Close(code, API_KEY), None))
        with pytest.raises(LogsSubscribeError) as raised:
            await harness.client.receive_once()
        assert_safe_error(raised.value)
        assert raised.value.retryable is retryable

    run(scenario())
    assert len(harness.connections) == 1
    assert harness.socket.closed == 1
    assert harness.rpc.calls == []


@pytest.mark.parametrize("stage", ["connect", "send", "handshake_receive", "receive"])
def test_network_errors_are_retryable_without_automatic_reconnect(session_factory, stage):
    harness = Harness(session_factory)
    error = ConnectionResetError(API_KEY)

    async def scenario():
        if stage == "connect":
            harness.connect_error = error
        elif stage == "send":
            harness.socket.send_error = error
        elif stage == "handshake_receive":
            harness.socket.incoming.append(error)
        if stage == "receive":
            await harness.client.start()
            harness.socket.incoming.append(error)
            operation = harness.client.receive_once()
        else:
            operation = harness.client.start()
        with pytest.raises(LogsSubscribeError) as raised:
            await operation
        assert_safe_error(raised.value)
        assert raised.value.retryable is True

    run(scenario())
    assert len(harness.connections) == 1


def test_receive_cancellation_closes_socket_and_retains_committed_cursor(session_factory):
    harness = Harness(session_factory)

    async def scenario():
        await harness.client.start()
        await harness.receive(notification(error="InstructionError"))
        harness.socket.incoming.append(Pause())
        task = asyncio.create_task(harness.client.receive_once())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert harness.socket.closed == 1
        assert harness.client.heartbeat.state == "disconnected"
        cursor = StreamCursorRepository(session_factory).load(
            "logs_subscribe", harness.settings.pump_program_id
        )
        assert cursor is not None and cursor.signature == SIGNATURE

    run(scenario())
