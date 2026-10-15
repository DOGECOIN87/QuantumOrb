"""Explicit, credential-safe Helius Parsed Streams discovery and consumption."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal, Protocol
from urllib.parse import urlsplit

from sqlalchemy.orm import Session, sessionmaker
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from app.config import Settings
from app.db.catalog import (
    CatalogSnapshotInput,
    CatalogSnapshotRepository,
    canonical_catalog_json,
    load_pinned_catalog,
)
from app.db.decode_outcomes import DecodeOutcomeRepository
from app.db.stream_cursors import StreamCursorRepository
from app.decoders import DecodePipelineError, Decoder, DecodeResult, DecoderInput
from app.integrations.parsed_stream_decoder import ParsedStreamsDecoder
from app.integrations.parsed_stream_models import (
    ProgramDescription,
    notification_envelope,
)
from app.logging_config import SafeError

_DOCS = "https://www.helius.dev/docs/parsed-streams/quickstart#discovery"
_ALLOWLISTS: dict[str, tuple[str, ...]] = {
    "pump": (
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
    ),
    "pump_amm": (
        "create_pool",
        "buy",
        "buy_v2",
        "buy_exact_quote_in",
        "buy_exact_quote_in_v2",
        "sell",
        "sell_v2",
        "multi_hop_swap",
    ),
}
_EVENTS: dict[str, set[str]] = {
    "pump": {
        "CreateEvent",
        "TradeEvent",
        "CompleteEvent",
        "PostCompleteBuyEvent",
        "CompletePumpAmmMigrationEvent",
    },
    "pump_amm": {"CreatePoolEvent", "BuyEvent", "SellEvent"},
}
_WS_LOGGER = logging.Logger("quantumorb.parsed_streams.transport")
_WS_LOGGER.disabled = True  # Library debug logs can contain authentication headers.


class ParsedStreamsError(SafeError):
    """Only fixed diagnostics and numeric RPC codes cross the transport boundary."""

    def __init__(self, code: str, *, rpc_code: int | None = None, retryable: bool = False) -> None:
        super().__init__(code, "Parsed Streams operation failed", retryable=retryable)
        self.rpc_code = rpc_code


def stream_error_retryable(error: BaseException) -> bool:
    """Classify transport failures without retaining arbitrary exception text."""
    if isinstance(error, SafeError):
        return error.retryable
    if isinstance(error, InvalidStatus):
        return error.response.status_code in {408, 429, 500, 502, 503, 504}
    if isinstance(error, ConnectionClosed):
        close = error.rcvd if error.rcvd is not None else error.sent
        return close is None or close.code in {1000, 1001, 1005, 1006}
    return isinstance(error, (OSError, TimeoutError))


class StreamSocket(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def ping(self) -> None: ...
    async def close(self) -> None: ...


class _WebSocket:
    def __init__(self, connection: ClientConnection) -> None:
        self._connection = connection

    async def send(self, message: str) -> None:
        await self._connection.send(message)

    async def recv(self) -> str | bytes:
        return await self._connection.recv()

    async def ping(self) -> None:
        pong = await self._connection.ping()
        await pong

    async def close(self) -> None:
        await self._connection.close()


class _NoRedirectConnect(connect):
    def process_redirect(self, exc: Exception) -> Exception | str:
        if isinstance(exc, InvalidStatus) and 300 <= exc.response.status_code < 400:
            return ParsedStreamsError("stream_redirect_rejected")
        return exc


async def connect_stream_socket(url: str, headers: Mapping[str, str]) -> StreamSocket:
    """Header authentication only; no redirect or query-string credential fallback."""
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "wss"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ParsedStreamsError("invalid_stream_endpoint")
        # Awaiting connect once does not enable its optional reconnect iterator.
        connection = await _NoRedirectConnect(
            url,
            additional_headers=headers,
            open_timeout=10,
            close_timeout=5,
            ping_interval=20,
            ping_timeout=20,
            max_size=4 * 1024 * 1024,
            max_queue=16,
            logger=_WS_LOGGER,
        )
        return _WebSocket(connection)
    except ParsedStreamsError:
        raise
    except InvalidStatus as error:
        status = error.response.status_code
        raise ParsedStreamsError(
            "stream_unauthorized" if status in {401, 403} else "stream_connection_failed",
            retryable=stream_error_retryable(error),
        ) from None
    except Exception as error:
        raise ParsedStreamsError(
            "stream_connection_failed", retryable=stream_error_retryable(error)
        ) from None


@dataclass(frozen=True)
class StreamHeartbeat:
    state: Literal["disconnected", "connecting", "subscribed", "receiving", "error"] = (
        "disconnected"
    )
    last_contact_at: datetime | None = None
    last_notification_at: datetime | None = None
    last_persisted_slot: int | None = None
    last_signature: str | None = None
    notifications_received: int = 0
    safe_error_code: str | None = None
    discovery_changes: int = 0


class ParsedStreamsClient:
    """One explicit connection session; reconnect and gap recovery belong to T020."""

    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session] | Callable[[], Session],
        *,
        connector: Callable[
            [str, Mapping[str, str]], Awaitable[StreamSocket]
        ] = connect_stream_socket,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        decoder_factory: Callable[[Mapping[str, ProgramDescription]], Decoder] | None = None,
        handshake_timeout: float = 10,
        receive_timeout: float = 45,
    ) -> None:
        if any(
            type(value) not in {int, float} or not math.isfinite(value) or value <= 0
            for value in (handshake_timeout, receive_timeout)
        ):
            raise ParsedStreamsError("invalid_stream_timeout")
        self._settings = settings
        self._connector = connector
        self._clock = clock
        self._decode_factory = decoder_factory
        self._handshake_timeout = handshake_timeout
        self._receive_timeout = receive_timeout
        self._cursors = StreamCursorRepository(session_factory)
        self._outcomes = DecodeOutcomeRepository(session_factory)
        self._catalog = CatalogSnapshotRepository(session_factory)
        self._discovery: StreamSocket | None = None
        self._socket: StreamSocket | None = None
        self._decoder: Decoder | None = None
        self._subscriptions: dict[int, str] = {}
        self._revisions: dict[str, str] = {}
        self._pending: deque[DecodeResult] = deque()
        self._request_id = 0
        self._heartbeat = StreamHeartbeat()

    @property
    def heartbeat(self) -> StreamHeartbeat:
        return self._heartbeat

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ParsedStreamsError("invalid_stream_clock")
        return value.astimezone(UTC)

    @staticmethod
    def _message(raw: str | bytes) -> dict[str, object]:
        def reject_constant(value: str) -> object:
            del value
            raise ValueError("non-finite JSON")

        def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON member")
                result[key] = value
            return result

        try:
            payload = json.loads(
                raw, parse_constant=reject_constant, object_pairs_hook=unique_object
            )
            if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
                raise ValueError("invalid RPC object")
            return payload
        except (TypeError, ValueError, UnicodeDecodeError, RecursionError):
            raise ParsedStreamsError("invalid_stream_message") from None

    async def _rpc(self, socket: StreamSocket, method: str, params: list[object]) -> object:
        self._request_id += 1
        request_id = self._request_id
        async with asyncio.timeout(self._handshake_timeout):
            await socket.send(
                json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            )
            while True:
                payload = self._message(await socket.recv())
                self._heartbeat = replace(self._heartbeat, last_contact_at=self._now())
                if payload.get("method") == "parsedTransactionNotification":
                    if socket is not self._socket:
                        raise ParsedStreamsError("unexpected_discovery_notification")
                    result = self._process_notification(payload)
                    if len(self._pending) >= 64:
                        raise ParsedStreamsError("stream_consumer_backpressure", retryable=True)
                    self._pending.append(result)
                    continue
                if type(payload.get("id")) is not int or payload["id"] != request_id:
                    raise ParsedStreamsError("stream_response_id_mismatch")
                if "error" in payload:
                    error = payload["error"]
                    if not isinstance(error, dict) or type(error.get("code")) is not int:
                        raise ParsedStreamsError("invalid_stream_rpc_error")
                    code = error["code"]
                    raise ParsedStreamsError(
                        "stream_rpc_error", rpc_code=code, retryable=code in {-32001, -32002}
                    )
                if set(payload) != {"jsonrpc", "id", "result"}:
                    raise ParsedStreamsError("invalid_stream_response")
                return payload["result"]

    async def start(self) -> None:
        if self._socket is not None or self._discovery is not None:
            raise ParsedStreamsError("stream_already_started")
        settings = self._settings
        if (
            settings.ingest_mode != "live"
            or settings.ingest_transport != "parsed_streams"
            or settings.helius_api_key is None
            or settings.solana_network != "mainnet-beta"
        ):
            raise ParsedStreamsError("stream_requires_explicit_live_mode")
        self._heartbeat = replace(self._heartbeat, state="connecting", safe_error_code=None)
        self._heartbeat = replace(self._heartbeat, discovery_changes=0)
        self._pending.clear()
        try:
            settings = Settings.model_validate(settings.model_dump(warnings=False))
            if settings.helius_api_key is None:
                raise ParsedStreamsError("stream_requires_explicit_live_mode")
            headers = {"x-api-key": settings.helius_api_key.get_secret_value()}
            pinned = load_pinned_catalog(settings)
            self._discovery = await asyncio.wait_for(
                self._connector(settings.helius_parsed_streams_discovery_url, headers),
                self._handshake_timeout,
            )
            descriptions: dict[str, ProgramDescription] = {}
            for snapshot in pinned:
                result = await self._rpc(
                    self._discovery, "describeProgram", [{"program": snapshot.program_id}]
                )
                if not isinstance(result, dict) or result.get("id") != snapshot.program_id:
                    raise ParsedStreamsError("stream_catalog_program_mismatch")
                serialized = canonical_catalog_json(result)
                digest = hashlib.sha256(serialized.encode()).hexdigest()
                self._catalog.insert_if_absent(
                    CatalogSnapshotInput(
                        program_id=snapshot.program_id,
                        program_name=snapshot.program_name,
                        idl_revision="helius-description:" + digest,
                        payload_json=serialized,
                        content_sha256=digest,
                        observed_at=self._now(),
                        source_url=_DOCS,
                    )
                )
                description = ProgramDescription.model_validate(result)
                local = json.loads(snapshot.payload_json)
                local_instructions = {entry["name"] for entry in local["instructions"]}
                local_events = {entry["name"] for entry in local["events"]}
                required = set(_ALLOWLISTS[snapshot.program_name])
                if not required <= local_instructions or not required <= set(
                    description.instructions
                ):
                    raise ParsedStreamsError("stream_instruction_catalog_mismatch")
                if not _EVENTS[snapshot.program_name] <= local_events or not _EVENTS[
                    snapshot.program_name
                ] <= set(description.events):
                    raise ParsedStreamsError("stream_event_catalog_mismatch")
                changes = len(
                    local_instructions.symmetric_difference(description.instructions)
                ) + len(local_events.symmetric_difference(description.events))
                self._heartbeat = replace(
                    self._heartbeat, discovery_changes=self._heartbeat.discovery_changes + changes
                )
                if changes:
                    logging.getLogger(__name__).warning(
                        "Parsed Streams program catalog differs from the pinned IDL"
                    )
                descriptions[snapshot.program_id] = description
                self._revisions[snapshot.program_id] = (
                    "t018:" + snapshot.idl_revision + ":" + digest
                )
            await asyncio.wait_for(self._discovery.close(), self._handshake_timeout)
            self._discovery = None
            self._decoder = (
                self._decode_factory(descriptions)
                if self._decode_factory is not None
                else ParsedStreamsDecoder(settings, descriptions)
            )
            self._socket = await asyncio.wait_for(
                self._connector(settings.helius_parsed_streams_url, headers),
                self._handshake_timeout,
            )
            for snapshot in pinned:
                result = await self._rpc(
                    self._socket,
                    "parsedTransactionSubscribe",
                    [
                        {
                            "programs": [snapshot.program_id],
                            "instructionNames": list(_ALLOWLISTS[snapshot.program_name]),
                            "includeCpi": True,
                            "includeFailed": False,
                        },
                        {"commitment": "confirmed", "details": "full"},
                    ],
                )
                if type(result) is not int or result < 0 or result in self._subscriptions:
                    raise ParsedStreamsError("invalid_stream_subscription")
                self._subscriptions[result] = snapshot.program_id
            self._heartbeat = replace(
                self._heartbeat, state="subscribed", last_contact_at=self._now()
            )
        except BaseException as error:
            await self.close()
            if isinstance(error, asyncio.CancelledError):
                raise
            safe = (
                error
                if isinstance(error, ParsedStreamsError)
                else ParsedStreamsError(
                    "stream_start_failed", retryable=stream_error_retryable(error)
                )
            )
            self._heartbeat = replace(self._heartbeat, state="error", safe_error_code=safe.code)
            raise safe from None

    def _process_notification(self, payload: dict[str, object]) -> DecodeResult:
        params = payload.get("params")
        subscription = params.get("subscription") if isinstance(params, dict) else None
        if type(subscription) is not int or subscription not in self._subscriptions:
            raise ParsedStreamsError("unknown_stream_subscription")
        program = self._subscriptions[subscription]
        envelope = notification_envelope(
            payload,
            subscription_id=subscription,
            received_at=self._now(),
            idl_revision=self._revisions[program],
        )
        raw = self._cursors.journal(envelope, program)
        slot = envelope.slot
        newer = (
            self._heartbeat.last_persisted_slot is None
            or slot >= self._heartbeat.last_persisted_slot
        )
        self._heartbeat = replace(
            self._heartbeat,
            last_notification_at=self._now(),
            last_persisted_slot=slot if newer else self._heartbeat.last_persisted_slot,
            last_signature=envelope.signature if newer else self._heartbeat.last_signature,
            notifications_received=self._heartbeat.notifications_received + 1,
        )
        if self._decoder is None:
            raise ParsedStreamsError("stream_decoder_not_ready")
        result = self._decoder.decode(
            DecoderInput(
                raw_event_id=raw.id, transport=raw.transport, payload_json=raw.payload_json
            )
        )
        if result.raw_event_id != raw.id:
            raise DecodePipelineError("stream decoder references another raw event")
        self._outcomes.record_outcome(
            raw_event_id=raw.id,
            status=result.status,
            safe_error_code=result.safe_error_code,
            safe_error_message=result.safe_error_message,
        )
        return result

    async def receive_once(self) -> DecodeResult:
        if self._pending:
            self._heartbeat = replace(self._heartbeat, state="receiving", safe_error_code=None)
            return self._pending.popleft()
        if self._socket is None:
            raise ParsedStreamsError("stream_not_started")
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(self._socket.recv(), self._receive_timeout)
                except TimeoutError:
                    await asyncio.wait_for(self._socket.ping(), self._handshake_timeout)
                    self._heartbeat = replace(self._heartbeat, last_contact_at=self._now())
                    continue
                payload = self._message(raw)
                self._heartbeat = replace(self._heartbeat, last_contact_at=self._now())
                result = self._process_notification(payload)
                self._heartbeat = replace(self._heartbeat, state="receiving", safe_error_code=None)
                return result
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception as error:
            safe = (
                error
                if isinstance(error, ParsedStreamsError)
                else ParsedStreamsError(
                    "stream_receive_failed", retryable=stream_error_retryable(error)
                )
            )
            self._heartbeat = replace(self._heartbeat, state="error", safe_error_code=safe.code)
            await self.close(preserve_error=True)
            raise safe from None

    async def close(self, *, preserve_error: bool = False) -> None:
        sockets = (self._socket, self._discovery)
        self._socket = self._discovery = None
        self._subscriptions.clear()
        self._pending.clear()
        self._decoder = None
        for socket in sockets:
            if socket is not None:
                try:
                    await asyncio.wait_for(socket.close(), self._handshake_timeout)
                except Exception:
                    pass
        if not preserve_error:
            self._heartbeat = replace(self._heartbeat, state="disconnected")
