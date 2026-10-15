"""Exercise reconnect recovery through the real journal, decoder, and canonical store."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import struct
from collections import Counter, deque
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from websockets.exceptions import ConnectionClosed
from websockets.frames import Close

from app.config import BASE58_ALPHABET, Settings
from app.db.canonical_events import CanonicalEventRepository
from app.db.models import job_runs, raw_events, scout_events
from app.db.session import create_all, create_database_engine, create_session_factory
from app.db.stream_cursors import StreamCursorRepository
from app.domain.enums import Commitment, DecodeStatus
from app.domain.keys import canonical_event_key
from app.integrations.logs_subscribe import LogsSubscribeClient
from app.integrations.raw_logs_decoder import RawLogsDecoder
from app.integrations.stream_recovery import BoundedStreamRecovery
from app.integrations.stream_supervisor import StreamSupervisor
from app.normalization import PumpCompletionMigrationNormalizer


def test_real_reconnect_pipeline_recovers_missed_event_and_deduplicates_overlap(tmp_path):
    now = datetime(2026, 10, 13, 12, tzinfo=UTC)
    settings = Settings(
        _env_file=None,
        app_env="test",
        ingest_mode="live",
        ingest_transport="logs_subscribe",
        helius_api_key="synthetic-reconnect-pipeline-secret",
    )
    engine = create_database_engine(f"sqlite:///{tmp_path / 'pipeline.db'}")
    create_all(engine)
    factory = create_session_factory(engine)
    cursors = StreamCursorRepository(factory)
    canonical = CanonicalEventRepository(factory)
    normalizer = PumpCompletionMigrationNormalizer(settings)

    def base58(data):
        number = int.from_bytes(data, "big")
        encoded = ""
        while number:
            number, digit = divmod(number, 58)
            encoded = BASE58_ALPHABET[digit] + encoded
        return "1" * (len(data) - len(data.lstrip(b"\0"))) + encoded

    signatures = {
        name: base58(number.to_bytes(64, "big"))
        for number, name in enumerate(["older", "A", "B", "C"], start=1)
    }
    slots = {
        signatures[name]: slot for name, slot in [("older", 99), ("A", 100), ("B", 101), ("C", 102)]
    }
    # Independently packed pinned CompleteEvent: discriminator, three pubkeys,
    # little-endian timestamp, then quote mint. No application encoder is mocked.
    binary = bytes([95, 114, 97, 156, 212, 46, 152, 8])
    binary += b"\x01" * 32 + b"\x02" * 32 + b"\x03" * 32
    binary += struct.pack("<q", int(now.timestamp())) + b"\x04" * 32
    logs = [
        f"Program {settings.pump_program_id} invoke [1]",
        "Program data: " + base64.b64encode(binary).decode(),
        f"Program {settings.pump_program_id} success",
    ]

    def transaction(signature):
        return {
            "slot": slots[signature],
            "blockTime": int(now.timestamp()),
            "version": "legacy",
            "transaction": {
                "signatures": [signature],
                "message": {
                    "accountKeys": [settings.pump_program_id],
                    "instructions": [{"programIdIndex": 0, "accounts": [], "data": ""}],
                },
            },
            "meta": {"err": None, "innerInstructions": [], "logMessages": logs},
        }

    def notification(name):
        signature = signatures[name]
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "logsNotification",
                "params": {
                    "subscription": 101,
                    "result": {
                        "context": {"slot": slots[signature]},
                        "value": {"signature": signature, "err": None, "logs": logs},
                    },
                },
            }
        )

    def rows(table):
        with factory() as session:
            return list(session.execute(select(table)).mappings())

    async def successful_runs(count):
        while True:
            completed = [row for row in rows(job_runs) if row["status"] == "success"]
            if len(completed) >= count:
                return completed
            await asyncio.sleep(0)

    async def scenario():
        a_committed, c_committed = asyncio.Event(), asyncio.Event()
        deliveries = []
        listing_calls = Counter()
        transaction_calls = Counter()
        sockets = []
        frozen_anchors = []

        class Socket:
            def __init__(self, connection):
                self.connection = connection
                self.responses = deque()
                self.notifications = 0
                self.closed = 0

            async def send(self, message):
                request = json.loads(message)
                assert request["method"] == "logsSubscribe"
                assert len(request["params"][0]["mentions"]) == 1
                self.responses.append(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": request["id"],
                            "result": request["id"] + 100,
                        }
                    )
                )

            async def recv(self):
                if self.responses:
                    return self.responses.popleft()
                if self.connection == 1:
                    if self.notifications == 0:
                        # Complete the real bootstrap audit before the first
                        # live event establishes the later reconnect anchor.
                        await successful_runs(1)
                        self.notifications += 1
                        return notification("A")
                    await a_committed.wait()
                    raise ConnectionClosed(Close(1001, "server restart"), None)
                if self.notifications == 0:
                    frozen = cursors.load("logs_subscribe", settings.pump_program_id)
                    assert frozen.anchor_slot == frozen.slot == 100
                    assert frozen.anchor_signature == signatures["A"]
                    frozen_anchors.append(frozen)
                    self.notifications += 1
                    return notification("C")
                await asyncio.Event().wait()

            async def ping(self):
                pass

            async def close(self):
                self.closed += 1

        class Rpc:
            async def rpc(self, method, params):
                if method == "getTransaction":
                    signature = params[0]
                    transaction_calls[signature] += 1
                    assert params[1]["maxSupportedTransactionVersion"] == 1
                    return {"result": copy.deepcopy(transaction(signature))}
                assert method == "getSignaturesForAddress"
                program = params[0]
                listing_calls[program] += 1
                if listing_calls[program] == 1 or program == settings.pump_amm_program_id:
                    return {"result": []}
                # Recovery starts after resubscription, then waits for C to
                # arrive through the actual live journal and canonical sink.
                await c_committed.wait()
                frozen = cursors.load("logs_subscribe", settings.pump_program_id)
                assert frozen.slot == 102 and frozen.anchor_slot == 100
                assert frozen.anchor_signature == signatures["A"]
                return {
                    "result": [
                        {
                            "signature": signatures[name],
                            "slot": slots[signatures[name]],
                            "err": None,
                            "memo": None,
                            "blockTime": int(now.timestamp()),
                            "confirmationStatus": "confirmed",
                        }
                        for name in ["C", "B", "A", "older"]
                    ]
                }

        rpc = Rpc()

        async def connector(url, headers):
            assert url == settings.helius_logs_ws_url
            assert set(headers) == {"x-api-key"}
            socket = Socket(len(sockets) + 1)
            sockets.append(socket)
            return socket

        def client_factory():
            return LogsSubscribeClient(
                settings,
                factory,
                rpc=rpc,
                connector=connector,
                decoder=RawLogsDecoder(settings),
                clock=lambda: now,
            )

        async def consume(result):
            assert result.status == DecodeStatus.DECODED
            raw = next(row for row in rows(raw_events) if row["id"] == result.raw_event_id)
            for event in result.events:
                canonical.insert_if_absent(
                    normalizer.normalize(
                        event,
                        provider="helius",
                        signature=raw["signature"],
                        slot=raw["slot"],
                        commitment=Commitment(raw["commitment"]),
                        observed_at=now,
                        raw_event_id=result.raw_event_id,
                    )
                )
                deliveries.append(raw["signature"])
            if raw["signature"] == signatures["A"]:
                a_committed.set()
            if raw["signature"] == signatures["C"]:
                c_committed.set()

        async def immediate_backoff(delay):
            assert 0.75 <= delay <= 1.25
            await asyncio.sleep(0)

        supervisor = StreamSupervisor(
            settings,
            factory,
            client_factory=client_factory,
            recovery=BoundedStreamRecovery(
                settings,
                factory,
                rpc=rpc,
                clock=lambda: now,
                decoder=RawLogsDecoder(settings),
            ),
            clock=lambda: now,
            sleeper=immediate_backoff,
            jitter=lambda low, high: 1,
        )
        runner = asyncio.create_task(supervisor.run(consume))
        try:
            audits = await asyncio.wait_for(successful_runs(2), timeout=5)
            assert len(sockets) == 2
            assert supervisor.status.reconnects == 1
            assert len(frozen_anchors) == 1
            assert all(
                program["covered"] for program in json.loads(audits[-1]["counts_json"])["programs"]
            )
            stored = rows(scout_events)
            assert {row["signature"] for row in stored} == {
                signatures[name] for name in ["A", "B", "C"]
            }
            assert len(stored) == 3
            assert {row["event_key"] for row in stored} == {
                canonical_event_key(
                    "helius", signatures[name], settings.pump_program_id, "CompleteEvent", 0
                )
                for name in ["A", "B", "C"]
            }
            assert Counter(deliveries) == {
                signatures["A"]: 2,
                signatures["B"]: 1,
                signatures["C"]: 2,
            }
            assert transaction_calls == Counter(deliveries)
            cursor = cursors.load("logs_subscribe", settings.pump_program_id)
            assert cursor.slot == 102 and cursor.signature == signatures["C"]
            assert cursor.pending_since is None and cursor.data_gap is False
            raw = next(row for row in rows(raw_events) if row["id"] == cursor.raw_event_id)
            assert json.loads(raw["payload_json"])["method"] == "logsNotification"
            assert cursors.load("logs_subscribe", settings.pump_amm_program_id).slot is None
        finally:
            runner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await runner
        assert all(socket.closed == 1 for socket in sockets)

    try:
        asyncio.run(scenario())
    finally:
        engine.dispose()
