"""Offline acceptance tests for discovery, subscriptions, and durable stream intake."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import traceback
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event, select
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response

from app.config import Settings
from app.db.catalog import load_pinned_catalog
from app.db.models import provider_catalog_snapshots, raw_events
from app.db.session import create_all, create_database_engine, create_session_factory
from app.db.stream_cursors import StreamCursorRepository
from app.decoders import DecodeResult, DecoderInput
from app.domain.enums import DecodeStatus
from app.integrations import parsed_streams as stream_module
from app.integrations.parsed_streams import ParsedStreamsClient, ParsedStreamsError

API_KEY = "synthetic-parsed-streams-test-secret"
SIGNATURE = "1" * 64
OBSERVED_AT = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
PUMP_INSTRUCTIONS = [
    "create",
    "create_v2",
    "buy",
    "buy_v2",
    "buy_v3",
    "buy_exact_quote_in_v2",
    "buy_exact_quote_in_v3",
    "sell",
    "sell_v2",
    "sell_v3",
    "migrate",
    "migrate_v2",
    "multi_hop_curve_swap",
]
AMM_INSTRUCTIONS = [
    "create_pool",
    "buy",
    "buy_v2",
    "buy_exact_quote_in",
    "buy_exact_quote_in_v2",
    "sell",
    "sell_v2",
    "multi_hop_swap",
]


def run(coroutine):
    return asyncio.run(coroutine)


def live_settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        **{
            "app_env": "test",
            "ingest_mode": "live",
            "ingest_transport": "parsed_streams",
            "helius_api_key": API_KEY,
            **overrides,
        },
    )


@pytest.fixture
def session_factory(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'stream.db'}")
    create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def stored_rows(session_factory, table):
    with session_factory() as observer:
        return list(observer.execute(select(table)).mappings())


def descriptions(settings: Settings) -> dict[str, dict[str, Any]]:
    result = {}
    for snapshot in load_pinned_catalog(settings):
        payload = json.loads(snapshot.payload_json)
        result[snapshot.program_id] = {
            "id": snapshot.program_id,
            "name": snapshot.program_name,
            "instructions": [entry["name"] for entry in payload["instructions"]],
            "events": [entry["name"] for entry in payload["events"]],
            "roles": sorted(
                {
                    account["name"]
                    for instruction in payload["instructions"]
                    for account in instruction["accounts"]
                }
            ),
        }
    return result


class Pause:
    """A cancellable receive window used to exercise the client's timeout path."""


class FakeSocket:
    def __init__(self, response_factory=None):
        self.sent: list[dict[str, Any]] = []
        self.incoming: deque[str | bytes | Exception | Pause] = deque()
        self.response_factory = response_factory
        self.closed = 0
        self.pings = 0
        self.send_error: Exception | None = None
        self.send_pause = False
        self.close_error: Exception | None = None
        self.ping_error: Exception | None = None
        self.before_response = None

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
            raise AssertionError("receive timeout must cancel the mock wait")
        return item

    async def ping(self) -> None:
        self.pings += 1
        if self.ping_error is not None:
            raise self.ping_error

    async def close(self) -> None:
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


class CapturingDecoder:
    def __init__(self, session_factory):
        self.session_factory = session_factory
        self.inputs: list[DecoderInput] = []
        self.error: Exception | None = None

    def decode(self, event: DecoderInput) -> DecodeResult:
        # A separate session proves the journal committed before calling the decoder.
        rows = stored_rows(self.session_factory, raw_events)
        persisted = next(row for row in rows if row["id"] == event.raw_event_id)
        assert persisted["payload_json"] == event.payload_json
        self.inputs.append(event)
        if self.error is not None:
            raise self.error
        return DecodeResult(
            raw_event_id=event.raw_event_id,
            status=DecodeStatus.RAW_ONLY,
            safe_error_code="test_unclassified",
            safe_error_message="mock decoder retains raw evidence",
        )


class Harness:
    def __init__(self, session_factory, *, settings=None, decoder=True, **client_options):
        self.settings = settings or live_settings()
        self.session_factory = session_factory
        self.programs = descriptions(self.settings)
        self.discovery = FakeSocket(
            lambda request: {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": copy.deepcopy(self.programs[request["params"][0]["program"]]),
            }
        )
        self.subscription = FakeSocket(
            lambda request: {"jsonrpc": "2.0", "id": request["id"], "result": request["id"] + 100}
        )
        self.connections: list[tuple[str, dict[str, str]]] = []
        self.connect_error: Exception | None = None
        self.decoder = CapturingDecoder(session_factory)
        self.factory_descriptions = None
        self.now = OBSERVED_AT

        async def connector(url, headers):
            self.connections.append((url, dict(headers)))
            if self.connect_error is not None:
                raise self.connect_error
            if url == self.settings.helius_parsed_streams_discovery_url:
                return self.discovery
            # Discovery must already be durable when subscriptions are created.
            assert len(stored_rows(session_factory, provider_catalog_snapshots)) == 2
            return self.subscription

        def decoder_factory(programs):
            assert len(stored_rows(session_factory, provider_catalog_snapshots)) == 2
            self.factory_descriptions = programs
            return self.decoder

        self.client = ParsedStreamsClient(
            self.settings,
            session_factory,
            connector=connector,
            clock=lambda: self.now,
            **({"decoder_factory": decoder_factory} if decoder else {}),
            **client_options,
        )


def assert_safe_error(error: ParsedStreamsError) -> None:
    assert error.code
    assert API_KEY not in str(error)
    assert API_KEY not in repr(error)
    assert API_KEY not in "".join(traceback.format_exception(error))


def notification(settings: Settings, *, subscription=103, slot=300_000_001):
    pump = next(item for item in load_pinned_catalog(settings) if item.program_name == "pump")
    payload = json.loads(pump.payload_json)
    create = next(item for item in payload["instructions"] if item["name"] == "create")
    return {
        "jsonrpc": "2.0",
        "method": "parsedTransactionNotification",
        "params": {
            "subscription": subscription,
            "result": {
                "context": {"slot": slot},
                "value": {
                    "transaction": {
                        "signature": SIGNATURE,
                        "slot": slot,
                        "blockTime": None,
                        "feePayer": "1" * 32,
                        "fee": 5000,
                        "accountKeys": ["1" * 32],
                        "status": "ok",
                        "error": None,
                        "summary": None,
                        "nativeTransfers": [],
                        "tokenTransfers": [],
                    },
                    "instructions": [
                        {
                            "instructionIndex": 4,
                            "innerInstructionIndex": None,
                            "stackHeight": 1,
                            "programId": settings.pump_program_id,
                            "programName": "pump",
                            "instructionName": "create",
                            "summary": None,
                            "decoded": {
                                "args": {
                                    "name": "Token",
                                    "symbol": "TOK",
                                    "uri": "https://example.invalid/token.json",
                                    "creator": "1" * 32,
                                },
                                "accounts": [
                                    {
                                        "name": account["name"],
                                        "pubkey": "1" * 32,
                                        "isSigner": False,
                                        "isWritable": False,
                                    }
                                    for account in create["accounts"]
                                ],
                            },
                        }
                    ],
                    "matchedIndexes": [0],
                },
            },
        },
    }


def test_start_discovers_durable_catalogs_then_subscribes_with_exact_verified_rpc(session_factory):
    harness = Harness(session_factory)
    assert harness.connections == []
    assert harness.client.heartbeat.state == "disconnected"
    assert harness.client.heartbeat.last_contact_at is None

    async def scenario():
        await harness.client.start()
        assert harness.client.heartbeat.state == "subscribed"
        assert harness.client.heartbeat.last_contact_at == OBSERVED_AT
        assert harness.client.heartbeat.last_notification_at is None
        assert harness.client.heartbeat.discovery_changes == 0
        await harness.client.close()

    run(scenario())
    assert harness.connections == [
        ("wss://fs-beta.helius-rpc.com/", {"x-api-key": API_KEY}),
        ("wss://beta.helius-rpc.com/", {"x-api-key": API_KEY}),
    ]
    assert all(API_KEY not in url for url, _headers in harness.connections)
    assert harness.discovery.sent == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "describeProgram",
            "params": [{"program": harness.settings.pump_program_id}],
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "describeProgram",
            "params": [{"program": harness.settings.pump_amm_program_id}],
        },
    ]
    assert harness.subscription.sent == [
        {
            "jsonrpc": "2.0",
            "id": index,
            "method": "parsedTransactionSubscribe",
            "params": [
                {
                    "programs": [program],
                    "instructionNames": instructions,
                    "includeCpi": True,
                    "includeFailed": False,
                },
                {"commitment": "confirmed", "details": "full"},
            ],
        }
        for index, program, instructions in [
            (3, harness.settings.pump_program_id, PUMP_INSTRUCTIONS),
            (4, harness.settings.pump_amm_program_id, AMM_INSTRUCTIONS),
        ]
    ]
    assert set(harness.factory_descriptions) == set(harness.programs)
    rows = stored_rows(session_factory, provider_catalog_snapshots)
    for row in rows:
        assert json.loads(row["payload_json"]) == harness.programs[row["program_id"]]
        assert row["content_sha256"] == hashlib.sha256(row["payload_json"].encode()).hexdigest()
        assert row["active"] is False
    assert harness.discovery.closed == 1
    assert harness.subscription.closed == 1
    assert harness.client.heartbeat.state == "disconnected"


@pytest.mark.parametrize(
    "settings",
    [
        Settings(_env_file=None, ingest_mode="demo", ingest_transport="fixture"),
        live_settings(ingest_transport="logs_subscribe"),
        live_settings(solana_network="devnet"),
        live_settings(solana_network="testnet"),
    ],
)
def test_only_explicit_mainnet_live_parsed_streams_can_connect(session_factory, settings):
    harness = Harness(session_factory, settings=settings)
    with pytest.raises(ParsedStreamsError) as raised:
        run(harness.client.start())
    assert_safe_error(raised.value)
    assert harness.connections == []
    assert stored_rows(session_factory, provider_catalog_snapshots) == []


@pytest.mark.parametrize("kind,name", [("instructions", "create"), ("events", "CreateEvent")])
def test_missing_required_catalog_name_stops_before_subscriptions(session_factory, kind, name):
    harness = Harness(session_factory)
    harness.programs[harness.settings.pump_program_id][kind].remove(name)
    with pytest.raises(ParsedStreamsError) as raised:
        run(harness.client.start())
    assert_safe_error(raised.value)
    assert len(harness.connections) == 1
    assert harness.subscription.sent == []
    assert harness.discovery.closed == 1
    # The changed description remains available for investigating the refusal.
    assert len(stored_rows(session_factory, provider_catalog_snapshots)) == 1


def test_new_provider_names_are_reported_but_never_added_to_filters(session_factory, caplog):
    harness = Harness(session_factory)
    harness.programs[harness.settings.pump_program_id]["instructions"].append("new_provider_method")
    harness.programs[harness.settings.pump_program_id]["events"].append("NewProviderEvent")

    async def scenario():
        await harness.client.start()
        assert harness.client.heartbeat.discovery_changes == 2
        assert "new_provider_method" not in json.dumps(harness.subscription.sent)
        await harness.client.close()

    run(scenario())
    assert "catalog" in caplog.text.lower()
    assert API_KEY not in caplog.text


def test_receive_journals_full_notification_before_decoder_and_records_outcome(session_factory):
    harness = Harness(session_factory)
    payload = notification(harness.settings)

    async def scenario():
        await harness.client.start()
        harness.subscription.incoming.append(json.dumps(payload).encode())
        result = await harness.client.receive_once()
        assert result.status == DecodeStatus.RAW_ONLY
        assert harness.client.heartbeat.state == "receiving"
        assert harness.client.heartbeat.last_notification_at == OBSERVED_AT
        assert harness.client.heartbeat.last_persisted_slot == 300_000_001
        assert harness.client.heartbeat.last_signature == SIGNATURE
        assert harness.client.heartbeat.notifications_received == 1
        await harness.client.close()

    run(scenario())
    rows = stored_rows(session_factory, raw_events)
    assert len(rows) == 1
    row = rows[0]
    assert json.loads(row["payload_json"]) == payload
    assert row["transport"] == "parsed_streams"
    assert row["commitment"] == "confirmed"
    assert row["signature"] == SIGNATURE
    assert row["slot"] == 300_000_001
    assert row["status"] == "raw_only"
    assert row["safe_error_code"] == "test_unclassified"
    assert len(harness.decoder.inputs) == 1


def test_default_decoder_returns_typed_instruction_from_durable_notification(session_factory):
    harness = Harness(session_factory, decoder=False)

    async def scenario():
        await harness.client.start()
        harness.subscription.incoming.append(json.dumps(notification(harness.settings)))
        result = await harness.client.receive_once()
        assert result.status == DecodeStatus.DECODED
        assert result.events == []
        assert result.instructions[0].instruction_name == "create"
        assert result.instructions[0].instruction_index == 4
        await harness.client.close()

    run(scenario())
    assert stored_rows(session_factory, raw_events)[0]["status"] == "decoded"


def test_unknown_notification_fields_are_kept_raw_without_decoded_output(session_factory):
    harness = Harness(session_factory, decoder=False)
    payload = notification(harness.settings)
    payload["params"]["result"]["value"]["futureProviderField"] = {"amount": "18446744073709551615"}

    async def scenario():
        await harness.client.start()
        harness.subscription.incoming.append(json.dumps(payload))
        result = await harness.client.receive_once()
        assert result.status == DecodeStatus.RAW_ONLY
        assert result.instructions == []
        assert result.events == []
        await harness.client.close()

    run(scenario())
    row = stored_rows(session_factory, raw_events)[0]
    assert json.loads(row["payload_json"]) == payload
    assert row["status"] == "raw_only"


def test_duplicate_notifications_are_idempotent_and_late_slots_do_not_rewind_heartbeat(
    session_factory,
):
    harness = Harness(session_factory)
    payload = notification(harness.settings, slot=200)
    late = notification(harness.settings, slot=100)

    async def scenario():
        await harness.client.start()
        for item in [payload, payload, late]:
            harness.subscription.incoming.append(json.dumps(item))
            await harness.client.receive_once()
        assert harness.client.heartbeat.last_persisted_slot == 200
        assert harness.client.heartbeat.notifications_received == 3
        await harness.client.close()

    run(scenario())
    assert len(stored_rows(session_factory, raw_events)) == 2


def test_notification_interleaved_with_second_subscription_reply_is_not_lost(session_factory):
    harness = Harness(session_factory)
    payload = notification(harness.settings)

    def before_response(request, socket):
        if request["id"] == 4:
            socket.incoming.append(json.dumps(payload))

    harness.subscription.before_response = before_response

    async def scenario():
        await harness.client.start()
        assert len(stored_rows(session_factory, raw_events)) == 1
        result = await harness.client.receive_once()
        assert result.raw_event_id == stored_rows(session_factory, raw_events)[0]["id"]
        assert len(harness.decoder.inputs) == 1
        await harness.client.close()

    run(scenario())


@pytest.mark.parametrize("stage", ["discovery", "subscription"])
def test_provider_rpc_error_is_correlated_safe_and_never_retried(session_factory, stage):
    harness = Harness(session_factory)
    target = getattr(harness, stage)
    target.response_factory = lambda request: {
        "jsonrpc": "2.0",
        "id": request["id"],
        "error": {"code": -32602, "message": API_KEY, "data": {"url": API_KEY}},
    }
    with pytest.raises(ParsedStreamsError) as raised:
        run(harness.client.start())
    assert_safe_error(raised.value)
    assert raised.value.rpc_code == -32602
    assert raised.value.retryable is False
    assert len(target.sent) == 1
    assert harness.discovery.closed == 1
    assert harness.subscription.closed == (1 if stage == "subscription" else 0)
    assert harness.client.heartbeat.state == "error"
    assert harness.client.heartbeat.safe_error_code == raised.value.code


@pytest.mark.parametrize("response_id", [None, True, "1", 999])
def test_discovery_response_id_must_match_request_integer(session_factory, response_id):
    harness = Harness(session_factory)
    harness.discovery.response_factory = lambda request: {
        "jsonrpc": "2.0",
        "id": response_id,
        "result": harness.programs[harness.settings.pump_program_id],
    }
    with pytest.raises(ParsedStreamsError) as raised:
        run(harness.client.start())
    assert_safe_error(raised.value)
    assert len(harness.connections) == 1
    assert stored_rows(session_factory, provider_catalog_snapshots) == []


@pytest.mark.parametrize("subscription_id", [None, True, "103", -1, 2.5])
def test_subscription_response_must_be_nonnegative_integer(session_factory, subscription_id):
    harness = Harness(session_factory)
    harness.subscription.response_factory = lambda request: {
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": subscription_id,
    }
    with pytest.raises(ParsedStreamsError) as raised:
        run(harness.client.start())
    assert_safe_error(raised.value)
    assert harness.subscription.closed == 1


def test_duplicate_subscription_ids_are_rejected(session_factory):
    harness = Harness(session_factory)
    harness.subscription.response_factory = lambda request: {
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": 103,
    }
    with pytest.raises(ParsedStreamsError) as raised:
        run(harness.client.start())
    assert_safe_error(raised.value)
    assert len(harness.subscription.sent) == 2
    assert harness.subscription.closed == 1


def test_description_of_wrong_program_is_rejected_before_catalog_insert(session_factory):
    harness = Harness(session_factory)
    harness.programs[harness.settings.pump_program_id]["id"] = harness.settings.pump_amm_program_id
    with pytest.raises(ParsedStreamsError) as raised:
        run(harness.client.start())
    assert_safe_error(raised.value)
    assert stored_rows(session_factory, provider_catalog_snapshots) == []
    assert harness.subscription.sent == []


@pytest.mark.parametrize("failure", ["connect", "send", "receive"])
def test_start_transport_failures_never_expose_original_exception(session_factory, failure):
    harness = Harness(session_factory)
    original = RuntimeError(f"provider exposed secret {API_KEY}")
    if failure == "connect":
        harness.connect_error = original
    elif failure == "send":
        harness.discovery.send_error = original
    else:
        harness.discovery.incoming.append(original)
    with pytest.raises(ParsedStreamsError) as raised:
        run(harness.client.start())
    assert_safe_error(raised.value)
    assert harness.client.heartbeat.state == "error"
    assert len(harness.connections) == 1


def test_handshake_timeout_closes_socket_and_leaves_safe_error(session_factory):
    harness = Harness(session_factory, handshake_timeout=0.001)
    harness.discovery.incoming.append(Pause())
    with pytest.raises(ParsedStreamsError) as raised:
        run(harness.client.start())
    assert_safe_error(raised.value)
    assert harness.discovery.closed == 1
    assert harness.subscription.sent == []


def test_handshake_timeout_includes_blocked_request_send(session_factory):
    harness = Harness(session_factory, handshake_timeout=0.001)
    harness.discovery.send_pause = True

    async def scenario():
        # An outer bound keeps a broken send-timeout implementation from hanging the test.
        with pytest.raises(ParsedStreamsError) as raised:
            await asyncio.wait_for(harness.client.start(), 0.1)
        assert_safe_error(raised.value)

    run(scenario())
    assert harness.discovery.closed == 1


def test_receive_timeout_checks_socket_pong_then_continues_without_rpc_heartbeat(session_factory):
    harness = Harness(session_factory, receive_timeout=0.001)

    async def scenario():
        await harness.client.start()
        harness.now += timedelta(seconds=45)
        harness.subscription.incoming.extend([Pause(), json.dumps(notification(harness.settings))])
        await harness.client.receive_once()
        assert harness.subscription.pings == 1
        assert harness.client.heartbeat.last_contact_at == harness.now
        assert harness.client.heartbeat.last_notification_at == harness.now
        assert len(harness.subscription.sent) == 2
        await harness.client.close()

    run(scenario())


def test_failed_pong_closes_stream_without_fabricating_contact(session_factory):
    harness = Harness(session_factory, receive_timeout=0.001)

    async def scenario():
        await harness.client.start()
        previous_contact = harness.client.heartbeat.last_contact_at
        harness.now += timedelta(seconds=45)
        harness.subscription.incoming.append(Pause())
        harness.subscription.ping_error = RuntimeError(API_KEY)
        with pytest.raises(ParsedStreamsError) as raised:
            await harness.client.receive_once()
        assert_safe_error(raised.value)
        assert harness.client.heartbeat.last_contact_at == previous_contact
        assert harness.client.heartbeat.last_notification_at is None
        assert harness.client.heartbeat.state == "error"

    run(scenario())
    assert harness.subscription.closed == 1


@pytest.mark.parametrize(
    "message",
    [
        "not-json",
        "[]",
        '{"jsonrpc":"2.0","method":"parsedTransactionNotification","params":NaN}',
        '{"jsonrpc":"1.0","params":{}}',
        b"\xff",
    ],
)
def test_invalid_notification_messages_close_safely_without_decoder(session_factory, message):
    harness = Harness(session_factory)

    async def scenario():
        await harness.client.start()
        harness.subscription.incoming.append(message)
        with pytest.raises(ParsedStreamsError) as raised:
            await harness.client.receive_once()
        assert_safe_error(raised.value)

    run(scenario())
    assert harness.decoder.inputs == []
    assert stored_rows(session_factory, raw_events) == []
    assert harness.subscription.closed == 1


def test_duplicate_json_keys_cannot_be_silently_collapsed_into_decoded_data(session_factory):
    harness = Harness(session_factory, decoder=False)
    payload = json.dumps(notification(harness.settings)).replace(
        '"method": "parsedTransactionNotification"',
        '"method": "parsedTransactionNotification", "method": "parsedTransactionNotification"',
    )

    async def scenario():
        await harness.client.start()
        harness.subscription.incoming.append(payload)
        with pytest.raises(ParsedStreamsError) as raised:
            await harness.client.receive_once()
        assert_safe_error(raised.value)

    run(scenario())
    assert stored_rows(session_factory, raw_events) == []


def test_notification_from_unknown_subscription_is_rejected_before_decoder(session_factory):
    harness = Harness(session_factory)

    async def scenario():
        await harness.client.start()
        harness.subscription.incoming.append(
            json.dumps(notification(harness.settings, subscription=9))
        )
        with pytest.raises(ParsedStreamsError) as raised:
            await harness.client.receive_once()
        assert_safe_error(raised.value)

    run(scenario())
    assert harness.decoder.inputs == []
    assert stored_rows(session_factory, raw_events) == []


def test_decoder_failure_preserves_committed_raw_evidence_and_safe_error(session_factory):
    harness = Harness(session_factory)
    payload = notification(harness.settings)
    harness.decoder.error = RuntimeError(API_KEY)

    async def scenario():
        await harness.client.start()
        harness.subscription.incoming.append(json.dumps(payload))
        with pytest.raises(ParsedStreamsError) as raised:
            await harness.client.receive_once()
        assert_safe_error(raised.value)
        assert harness.client.heartbeat.last_persisted_slot == 300_000_001
        assert harness.client.heartbeat.state == "error"

    run(scenario())
    row = stored_rows(session_factory, raw_events)[0]
    assert json.loads(row["payload_json"]) == payload
    assert row["status"] == "new"
    assert harness.subscription.closed == 1


def test_database_journal_failure_prevents_decoder_and_persisted_heartbeat(session_factory):
    harness = Harness(session_factory)

    async def scenario():
        await harness.client.start()
        # Simulate the real persistence boundary failing, rather than mocking the decoder.
        with session_factory() as session:
            session.execute(raw_events.delete())
            session.commit()
            raw_events.drop(session.connection())
            session.commit()
        harness.subscription.incoming.append(json.dumps(notification(harness.settings)))
        with pytest.raises(ParsedStreamsError) as raised:
            await harness.client.receive_once()
        assert_safe_error(raised.value)
        assert harness.client.heartbeat.last_persisted_slot is None
        assert harness.client.heartbeat.notifications_received == 0

    run(scenario())
    assert harness.decoder.inputs == []
    assert harness.subscription.closed == 1


def test_shutdown_is_idempotent_even_when_socket_close_raises(session_factory):
    harness = Harness(session_factory)

    async def scenario():
        await harness.client.start()
        harness.subscription.close_error = RuntimeError(API_KEY)
        await harness.client.close()
        await harness.client.close()
        with pytest.raises(ParsedStreamsError) as raised:
            await harness.client.receive_once()
        assert_safe_error(raised.value)
        assert harness.client.heartbeat.state == "disconnected"

    run(scenario())
    assert harness.discovery.closed == 1
    assert harness.subscription.closed == 1


def test_close_discards_pending_notification_results(session_factory):
    harness = Harness(session_factory)

    def before_response(request, socket):
        if request["id"] == 4:
            socket.incoming.append(json.dumps(notification(harness.settings)))

    harness.subscription.before_response = before_response

    async def scenario():
        await harness.client.start()
        await harness.client.close()
        with pytest.raises(ParsedStreamsError):
            await harness.client.receive_once()

    run(scenario())


def test_repeated_start_requires_explicit_close(session_factory):
    harness = Harness(session_factory)

    async def scenario():
        await harness.client.start()
        with pytest.raises(ParsedStreamsError) as raised:
            await harness.client.start()
        assert_safe_error(raised.value)
        assert harness.client.heartbeat.state == "subscribed"
        assert len(harness.connections) == 2
        await harness.client.close()

    run(scenario())


def test_explicit_restart_rediscovers_idempotently_and_uses_fresh_rpc_ids(session_factory):
    harness = Harness(session_factory)

    async def scenario():
        await harness.client.start()
        await harness.client.close()
        await harness.client.start()
        assert harness.client.heartbeat.state == "subscribed"
        assert harness.client.heartbeat.discovery_changes == 0
        assert len(stored_rows(session_factory, provider_catalog_snapshots)) == 2
        assert [request["id"] for request in harness.discovery.sent] == [1, 2, 5, 6]
        assert [request["id"] for request in harness.subscription.sent] == [3, 4, 7, 8]
        harness.subscription.incoming.append(
            json.dumps(notification(harness.settings, subscription=107))
        )
        await harness.client.receive_once()
        await harness.client.close()

    run(scenario())
    assert harness.discovery.closed == 2
    assert harness.subscription.closed == 2


@pytest.mark.parametrize("field", ["handshake_timeout", "receive_timeout"])
@pytest.mark.parametrize("value", [0, -1, True, float("nan"), float("inf"), "10"])
def test_invalid_timeout_configuration_is_rejected_without_connecting(
    session_factory, field, value
):
    with pytest.raises(ParsedStreamsError) as raised:
        Harness(session_factory, **{field: value})
    assert_safe_error(raised.value)


def test_default_connector_passes_auth_only_in_headers_and_disables_library_logs(monkeypatch):
    calls = []
    underlying = FakeSocket()
    underlying.incoming.append(b"wire notification")

    async def native_ping():
        underlying.pings += 1
        pong = asyncio.get_running_loop().create_future()
        pong.set_result(None)
        return pong

    underlying.ping = native_ping

    async def connect(url, **options):
        calls.append((url, options))
        return underlying

    monkeypatch.setattr(stream_module, "_NoRedirectConnect", connect)

    async def scenario():
        socket = await stream_module.connect_stream_socket(
            "wss://stream.example.invalid/", {"x-api-key": API_KEY}
        )
        await socket.send("wire request")
        assert await socket.recv() == b"wire notification"
        await socket.ping()
        await socket.close()

    # This native fixture accepts plain text to verify the adapter independently of JSON-RPC.
    async def native_send(message):
        assert message == "wire request"

    underlying.send = native_send
    run(scenario())
    assert len(calls) == 1
    url, options = calls[0]
    assert API_KEY not in url
    assert options["additional_headers"] == {"x-api-key": API_KEY}
    assert options["logger"].disabled is True
    assert options["open_timeout"] > 0
    assert underlying.pings == 1
    assert underlying.closed == 1


@pytest.mark.parametrize(
    "status,expected_retry",
    [
        (400, False),
        (401, False),
        (403, False),
        (404, False),
        (408, True),
        (409, False),
        (422, False),
        (429, True),
        (500, True),
        (501, False),
        (503, True),
    ],
)
def test_default_connector_http_failures_are_safe_and_never_retry_auth(
    monkeypatch, status, expected_retry
):
    calls = []

    async def connect(url, **options):
        calls.append(url)
        response = Response(status, "Provider error", Headers({"x-provider-secret": API_KEY}))
        raise InvalidStatus(response)

    monkeypatch.setattr(stream_module, "_NoRedirectConnect", connect)
    with pytest.raises(ParsedStreamsError) as raised:
        run(
            stream_module.connect_stream_socket(
                "wss://stream.example.invalid/", {"x-api-key": API_KEY}
            )
        )
    assert_safe_error(raised.value)
    assert raised.value.retryable is expected_retry
    if status in {401, 403}:
        assert raised.value.code == "stream_unauthorized"
    assert len(calls) == 1


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_default_connector_refuses_redirects_before_credentials_can_be_forwarded(status):
    connector = stream_module._NoRedirectConnect("wss://stream.example.invalid/")
    response = Response(
        status,
        "Redirect",
        Headers({"Location": "wss://other.example.invalid/", "x-provider-secret": API_KEY}),
    )
    result = connector.process_redirect(InvalidStatus(response))
    assert isinstance(result, ParsedStreamsError)
    assert result.code == "stream_redirect_rejected"
    assert result.retryable is False
    assert_safe_error(result)


@pytest.mark.parametrize(
    "url",
    [
        "ws://stream.example.invalid/",
        "https://stream.example.invalid/",
        "wss://user:password@stream.example.invalid/",
        "wss://stream.example.invalid/?api-key=untrusted",
        "wss://stream.example.invalid/#untrusted",
        "wss:///missing-host",
    ],
)
def test_default_connector_rejects_credential_bearing_or_invalid_endpoints(monkeypatch, url):
    calls = []

    async def connect(url, **options):
        calls.append(url)
        raise AssertionError("invalid URL must not reach the network")

    monkeypatch.setattr(stream_module, "_NoRedirectConnect", connect)
    with pytest.raises(ParsedStreamsError) as raised:
        run(stream_module.connect_stream_socket(url, {"x-api-key": API_KEY}))
    assert_safe_error(raised.value)
    assert calls == []


def test_notifications_atomically_track_independent_program_cursors(session_factory):
    harness = Harness(session_factory)
    repository = StreamCursorRepository(session_factory)

    async def scenario():
        await harness.client.start()
        for subscription, slot in [(103, 300_000_010), (104, 300_000_005), (103, 300_000_001)]:
            harness.subscription.incoming.append(
                json.dumps(notification(harness.settings, subscription=subscription, slot=slot))
            )
            result = await harness.client.receive_once()
            assert result.status == DecodeStatus.RAW_ONLY
        pump = repository.load("parsed_streams", harness.settings.pump_program_id)
        amm = repository.load("parsed_streams", harness.settings.pump_amm_program_id)
        assert pump is not None and amm is not None
        assert pump.slot == 300_000_010
        assert amm.slot == 300_000_005
        rows = stored_rows(session_factory, raw_events)
        assert pump.raw_event_id == rows[0]["id"]
        assert amm.raw_event_id == rows[1]["id"]
        assert pump.signature == amm.signature == SIGNATURE
        assert pump.received_at == amm.received_at == OBSERVED_AT
        await harness.client.close()

    run(scenario())


def test_handshake_notification_persists_program_cursor_before_start_returns(session_factory):
    harness = Harness(session_factory)

    def before_response(request, socket):
        if request["id"] == 4:
            socket.incoming.append(json.dumps(notification(harness.settings)))

    harness.subscription.before_response = before_response

    async def scenario():
        await harness.client.start()
        repository = StreamCursorRepository(session_factory)
        pump = repository.load("parsed_streams", harness.settings.pump_program_id)
        assert pump is not None
        assert pump.raw_event_id == stored_rows(session_factory, raw_events)[0]["id"]
        assert repository.load("parsed_streams", harness.settings.pump_amm_program_id) is None
        assert (await harness.client.receive_once()).raw_event_id == pump.raw_event_id
        await harness.client.close()

    run(scenario())


def test_cursor_write_failure_rolls_back_notification_before_decode(session_factory):
    harness = Harness(session_factory)
    engine = session_factory.kw["bind"]

    def reject_cursor(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO cursors"):
            raise RuntimeError(API_KEY)

    async def scenario():
        await harness.client.start()
        harness.subscription.incoming.append(json.dumps(notification(harness.settings)))
        with pytest.raises(ParsedStreamsError) as raised:
            await harness.client.receive_once()
        assert_safe_error(raised.value)
        assert raised.value.retryable is False
        assert harness.client.heartbeat.notifications_received == 0

    event.listen(engine, "before_cursor_execute", reject_cursor)
    try:
        run(scenario())
    finally:
        event.remove(engine, "before_cursor_execute", reject_cursor)
    assert stored_rows(session_factory, raw_events) == []
    assert harness.decoder.inputs == []
    assert (
        StreamCursorRepository(session_factory).load(
            "parsed_streams", harness.settings.pump_program_id
        )
        is None
    )


@pytest.mark.parametrize("stage", ["discovery", "subscription"])
def test_connect_timeout_is_bounded_retryable_and_does_not_fail_over(session_factory, stage):
    harness = Harness(session_factory, handshake_timeout=0.005)
    original = harness.client._connector
    cancelled = []

    async def blocked_connector(url, headers):
        blocked_url = (
            harness.settings.helius_parsed_streams_discovery_url
            if stage == "discovery"
            else harness.settings.helius_parsed_streams_url
        )
        if url == blocked_url:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.append(url)
                raise
        return await original(url, headers)

    harness.client._connector = blocked_connector
    with pytest.raises(ParsedStreamsError) as raised:
        run(asyncio.wait_for(harness.client.start(), 0.2))
    assert_safe_error(raised.value)
    assert raised.value.retryable is True
    assert len(cancelled) == 1
    assert len(harness.connections) == (0 if stage == "discovery" else 1)
    assert harness.subscription.sent == []


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
def test_close_codes_preserve_retry_policy_without_reconnecting(session_factory, code, retryable):
    harness = Harness(session_factory)

    async def scenario():
        await harness.client.start()
        harness.subscription.incoming.append(ConnectionClosed(Close(code, API_KEY), None))
        with pytest.raises(ParsedStreamsError) as raised:
            await harness.client.receive_once()
        assert_safe_error(raised.value)
        assert raised.value.retryable is retryable

    run(scenario())
    assert len(harness.connections) == 2
    assert harness.subscription.closed == 1


@pytest.mark.parametrize("stage", ["connect", "send", "handshake_receive", "receive"])
def test_network_reset_is_retryable_at_every_transport_boundary(session_factory, stage):
    harness = Harness(session_factory)
    error = ConnectionResetError(API_KEY)

    async def scenario():
        if stage == "connect":
            harness.connect_error = error
        elif stage == "send":
            harness.discovery.send_error = error
        elif stage == "handshake_receive":
            harness.discovery.incoming.append(error)
        if stage == "receive":
            await harness.client.start()
            harness.subscription.incoming.append(error)
            operation = harness.client.receive_once()
        else:
            operation = harness.client.start()
        with pytest.raises(ParsedStreamsError) as raised:
            await operation
        assert_safe_error(raised.value)
        assert raised.value.retryable is True

    run(scenario())
    assert len(harness.connections) == (2 if stage == "receive" else 1)


def test_receive_cancellation_closes_subscription_and_retains_durable_cursor(session_factory):
    harness = Harness(session_factory)

    async def scenario():
        await harness.client.start()
        harness.subscription.incoming.append(json.dumps(notification(harness.settings)))
        await harness.client.receive_once()
        harness.subscription.incoming.append(Pause())
        task = asyncio.create_task(harness.client.receive_once())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert harness.subscription.closed == 1
        assert harness.client.heartbeat.state == "disconnected"
        cursor = StreamCursorRepository(session_factory).load(
            "parsed_streams", harness.settings.pump_program_id
        )
        assert cursor is not None and cursor.signature == SIGNATURE

    run(scenario())


@pytest.mark.parametrize("sent_code,retryable", [(None, True), (1008, False), (1009, False)])
def test_unanswered_close_uses_local_code_or_network_loss(session_factory, sent_code, retryable):
    harness = Harness(session_factory)

    async def scenario():
        await harness.client.start()
        sent = None if sent_code is None else Close(sent_code, API_KEY)
        harness.subscription.incoming.append(ConnectionClosed(None, sent))
        with pytest.raises(ParsedStreamsError) as raised:
            await harness.client.receive_once()
        assert_safe_error(raised.value)
        assert raised.value.retryable is retryable

    run(scenario())
