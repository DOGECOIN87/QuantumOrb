"""Explicit logsSubscribe fallback with durable, versioned transaction decoding."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import StrictStr, ValidationError, field_validator
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.catalog import load_pinned_catalog
from app.db.decode_outcomes import DecodeOutcomeConflictError, DecodeOutcomeRepository
from app.db.raw_journal import RawEventRecord, RawJournal
from app.db.stream_cursors import StreamCursorRepository
from app.decoders import Decoder, DecodeResult, DecoderInput
from app.domain.enums import Commitment, DecodeStatus, Transport
from app.domain.models import DomainModel, JsonValue, NonNegativeInt, ProviderEnvelope, _signature
from app.integrations.helius_http import HeliusHttpClient
from app.integrations.parsed_streams import (
    ParsedStreamsClient,
    StreamHeartbeat,
    StreamSocket,
    connect_stream_socket,
    stream_error_retryable,
)
from app.integrations.raw_logs_decoder import RawLogsDecoder
from app.logging_config import SafeError


class LogsSubscribeError(SafeError):
    def __init__(self, code: str, *, rpc_code: int | None = None, retryable: bool = False) -> None:
        super().__init__(code, "Logs fallback operation failed", retryable=retryable)
        self.rpc_code = rpc_code


class RpcReader(Protocol):
    async def rpc(
        self, method: str, params: list[Any] | dict[str, Any] | None = None
    ) -> dict[str, Any]: ...


class _LogValue(DomainModel):
    signature: StrictStr
    err: JsonValue | None
    logs: list[StrictStr]

    _signature_check = field_validator("signature")(_signature)


class _Context(DomainModel):
    slot: NonNegativeInt


class _LogResult(DomainModel):
    context: _Context
    value: _LogValue


class _Params(DomainModel):
    subscription: NonNegativeInt
    result: _LogResult


class _Notification(DomainModel):
    jsonrpc: Literal["2.0"]
    method: Literal["logsNotification"]
    params: _Params


class LogsSubscribeClient:
    """Explicit one-session fallback; no startup ingestion or automatic failover."""

    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session] | Callable[[], Session],
        *,
        rpc: RpcReader | None = None,
        connector: Callable[
            [str, Mapping[str, str]], Awaitable[StreamSocket]
        ] = connect_stream_socket,
        decoder: Decoder | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        handshake_timeout: float = 10,
        receive_timeout: float = 45,
        fetch_timeout: float = 45,
    ) -> None:
        if any(
            type(value) not in {int, float} or not math.isfinite(value) or value <= 0
            for value in (handshake_timeout, receive_timeout, fetch_timeout)
        ):
            raise LogsSubscribeError("invalid_logs_timeout")
        self._settings = settings
        self._connector = connector
        self._clock = clock
        self._handshake_timeout = handshake_timeout
        self._receive_timeout = receive_timeout
        self._fetch_timeout = fetch_timeout
        self._rpc = rpc
        self._owns_rpc = rpc is None
        self._owned_rpc: HeliusHttpClient | None = None
        self._decoder = decoder
        self._journal = RawJournal(session_factory)
        self._cursors = StreamCursorRepository(session_factory)
        self._outcomes = DecodeOutcomeRepository(session_factory)
        self._socket: StreamSocket | None = None
        self._subscriptions: dict[int, str] = {}
        self._pending: deque[DecodeResult] = deque()
        self._request_id = 0
        self._revision = ""
        self._heartbeat = StreamHeartbeat()

    @property
    def heartbeat(self) -> StreamHeartbeat:
        return self._heartbeat

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise LogsSubscribeError("invalid_logs_clock")
        return value.astimezone(UTC)

    async def _request(self, method: str, params: list[object]) -> object:
        if self._socket is None:
            raise LogsSubscribeError("logs_not_started")
        self._request_id += 1
        request_id = self._request_id
        async with asyncio.timeout(self._handshake_timeout):
            await self._socket.send(
                json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            )
            while True:
                payload = ParsedStreamsClient._message(await self._socket.recv())
                self._heartbeat = replace(self._heartbeat, last_contact_at=self._now())
                if payload.get("method") == "logsNotification":
                    result = await self._process_notification(payload)
                    if len(self._pending) >= 64:
                        raise LogsSubscribeError("logs_consumer_backpressure", retryable=True)
                    self._pending.append(result)
                    continue
                if type(payload.get("id")) is not int or payload["id"] != request_id:
                    raise LogsSubscribeError("logs_response_id_mismatch")
                if "error" in payload:
                    error = payload["error"]
                    if not isinstance(error, dict) or type(error.get("code")) is not int:
                        raise LogsSubscribeError("invalid_logs_rpc_error")
                    code = error["code"]
                    raise LogsSubscribeError(
                        "logs_rpc_error", rpc_code=code, retryable=code in {-32001, -32002}
                    )
                if set(payload) != {"jsonrpc", "id", "result"}:
                    raise LogsSubscribeError("invalid_logs_response")
                return payload["result"]

    async def start(self) -> None:
        if self._socket is not None:
            raise LogsSubscribeError("logs_already_started")
        settings = self._settings
        if (
            settings.ingest_mode != "live"
            or settings.ingest_transport != "logs_subscribe"
            or settings.solana_network != "mainnet-beta"
        ):
            raise LogsSubscribeError("logs_requires_explicit_live_mode")
        self._heartbeat = replace(self._heartbeat, state="connecting", safe_error_code=None)
        try:
            settings = Settings.model_validate(settings.model_dump(warnings=False))
            if settings.helius_api_key is None:
                raise LogsSubscribeError("logs_requires_explicit_live_mode")
            pinned = load_pinned_catalog(settings)
            self._revision = "t019:" + pinned[0].idl_revision
            if self._decoder is None:
                self._decoder = RawLogsDecoder(settings)
            if self._owns_rpc:
                self._owned_rpc = HeliusHttpClient(settings.helius_api_key, settings.helius_rpc_url)
                self._rpc = self._owned_rpc
            self._socket = await asyncio.wait_for(
                self._connector(
                    settings.helius_logs_ws_url,
                    {"x-api-key": settings.helius_api_key.get_secret_value()},
                ),
                self._handshake_timeout,
            )
            for program in (settings.pump_program_id, settings.pump_amm_program_id):
                response = await self._request(
                    "logsSubscribe", [{"mentions": [program]}, {"commitment": "confirmed"}]
                )
                if type(response) is not int or response < 0 or response in self._subscriptions:
                    raise LogsSubscribeError("invalid_logs_subscription")
                self._subscriptions[response] = program
            self._heartbeat = replace(
                self._heartbeat, state="subscribed", last_contact_at=self._now()
            )
        except BaseException as error:
            await self.close()
            if isinstance(error, asyncio.CancelledError):
                raise
            safe = (
                error
                if isinstance(error, LogsSubscribeError)
                else LogsSubscribeError(
                    "logs_start_failed", retryable=stream_error_retryable(error)
                )
            )
            self._heartbeat = replace(self._heartbeat, state="error", safe_error_code=safe.code)
            raise safe from None

    def _envelope(
        self,
        payload: dict[str, Any],
        signature: str,
        slot: int,
        prefix: str,
        error: JsonValue | None = None,
    ) -> ProviderEnvelope:
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        return ProviderEnvelope(
            provider="helius",
            transport=Transport.LOGS_SUBSCRIBE,
            notification_id=prefix + ":" + digest,
            received_at=self._now(),
            commitment=Commitment.CONFIRMED,
            slot=slot,
            signature=signature,
            transaction_error=error,
            raw_payload_sha256=digest,
            idl_revision=self._revision,
            raw_payload=payload,
        )

    @staticmethod
    def _raw_only(raw_id: int, code: str) -> DecodeResult:
        return DecodeResult(
            raw_event_id=raw_id,
            status=DecodeStatus.RAW_ONLY,
            safe_error_code=code,
            safe_error_message="logs fallback observation could not be decoded safely",
        )

    def _record(self, raw_id: int, result: DecodeResult) -> None:
        try:
            self._outcomes.record_outcome(
                raw_event_id=raw_id,
                status=result.status,
                safe_error_code=result.safe_error_code,
                safe_error_message=result.safe_error_message,
            )
        except DecodeOutcomeConflictError:
            if result.status == DecodeStatus.DECODED:
                raise
            # A later unsuccessful retry cannot erase an earlier successful decode.

    async def _process_notification(self, payload: dict[str, object]) -> DecodeResult:
        try:
            if payload.get("method") != "logsNotification":
                raise ValueError
            params = payload["params"]
            if (
                not isinstance(params, dict)
                or type(params.get("subscription")) is not int
                or params["subscription"] not in self._subscriptions
            ):
                raise ValueError
            context = params["result"]["context"]
            value = params["result"]["value"]
            slot, signature = context["slot"], value["signature"]
            if type(slot) is not int or slot < 0 or not isinstance(signature, str):
                raise ValueError
            _signature(signature)
            envelope = self._envelope(
                payload, signature, slot, "logs-notification", value.get("err")
            )
        except (KeyError, TypeError, ValueError, ValidationError):
            raise LogsSubscribeError("invalid_logs_notification_identity") from None
        raw_logs = self._cursors.journal(envelope, self._subscriptions[params["subscription"]])
        newer = (
            self._heartbeat.last_persisted_slot is None
            or slot >= self._heartbeat.last_persisted_slot
        )
        self._heartbeat = replace(
            self._heartbeat,
            last_notification_at=self._now(),
            last_persisted_slot=slot if newer else self._heartbeat.last_persisted_slot,
            last_signature=signature if newer else self._heartbeat.last_signature,
            notifications_received=self._heartbeat.notifications_received + 1,
        )
        try:
            notification = _Notification.model_validate(payload)
        except ValidationError:
            result = self._raw_only(raw_logs.id, "unsupported_logs_notification")
            self._record(raw_logs.id, result)
            return result
        if notification.params.result.value.err is not None:
            result = self._raw_only(raw_logs.id, "failed_transaction")
            self._record(raw_logs.id, result)
            return result
        return await self._fetch_and_decode(raw_logs, signature, slot)

    async def _fetch_and_decode(
        self, raw_logs: RawEventRecord, signature: str, slot: int
    ) -> DecodeResult:
        if self._rpc is None:
            raise LogsSubscribeError("logs_rpc_not_ready")
        try:
            response = await asyncio.wait_for(
                self._rpc.rpc(
                    "getTransaction",
                    [
                        signature,
                        {
                            "commitment": "confirmed",
                            "encoding": "json",
                            "maxSupportedTransactionVersion": 1,
                        },
                    ],
                ),
                self._fetch_timeout,
            )
        except Exception:
            result = self._raw_only(raw_logs.id, "transaction_fetch_failed")
            self._record(raw_logs.id, result)
            return result
        if (
            not isinstance(response, dict)
            or set(response) != {"result"}
            or not isinstance(response["result"], dict)
        ):
            result = self._raw_only(raw_logs.id, "transaction_unavailable")
            self._record(raw_logs.id, result)
            return result
        transaction = response["result"]
        meta = transaction.get("meta")
        try:
            envelope = self._envelope(
                transaction,
                signature,
                slot,
                "logs-transaction",
                meta.get("err") if isinstance(meta, dict) else None,
            )
        except (ValueError, TypeError, RecursionError, ValidationError):
            result = self._raw_only(raw_logs.id, "invalid_transaction_response")
            self._record(raw_logs.id, result)
            return result
        raw_transaction = self._journal.insert(envelope)
        tx = transaction.get("transaction")
        signatures = tx.get("signatures") if isinstance(tx, dict) else None
        if (
            type(transaction.get("slot")) is not int
            or transaction["slot"] != slot
            or not isinstance(signatures, list)
            or not signatures
            or signatures[0] != signature
        ):
            result = self._raw_only(raw_transaction.id, "transaction_identity_mismatch")
        elif not isinstance(meta, dict) or "err" not in meta:
            result = self._raw_only(raw_transaction.id, "transaction_metadata_unavailable")
        elif meta["err"] is not None:
            result = self._raw_only(raw_transaction.id, "failed_transaction")
        else:
            try:
                if self._decoder is None:
                    raise ValueError
                result = self._decoder.decode(
                    DecoderInput(
                        raw_event_id=raw_transaction.id,
                        transport=raw_transaction.transport,
                        payload_json=raw_transaction.payload_json,
                    )
                )
                result = DecodeResult.model_validate(result.model_dump(warnings=False))
                if result.raw_event_id != raw_transaction.id:
                    raise ValueError
            except Exception:
                result = self._raw_only(raw_transaction.id, "transaction_decode_failed")
        self._record(raw_transaction.id, result)
        self._record(
            raw_logs.id,
            result
            if result.status == DecodeStatus.DECODED
            else self._raw_only(raw_logs.id, result.safe_error_code or "transaction_decode_failed"),
        )
        return result

    async def receive_once(self) -> DecodeResult:
        if self._pending:
            self._heartbeat = replace(self._heartbeat, state="receiving", safe_error_code=None)
            return self._pending.popleft()
        if self._socket is None:
            raise LogsSubscribeError("logs_not_started")
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(self._socket.recv(), self._receive_timeout)
                except TimeoutError:
                    await asyncio.wait_for(self._socket.ping(), self._handshake_timeout)
                    self._heartbeat = replace(self._heartbeat, last_contact_at=self._now())
                    continue
                payload = ParsedStreamsClient._message(raw)
                self._heartbeat = replace(self._heartbeat, last_contact_at=self._now())
                result = await self._process_notification(payload)
                self._heartbeat = replace(
                    self._heartbeat, state="receiving", safe_error_code=result.safe_error_code
                )
                return result
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception as error:
            safe = (
                error
                if isinstance(error, LogsSubscribeError)
                else LogsSubscribeError(
                    "logs_receive_failed",
                    retryable=stream_error_retryable(error),
                )
            )
            self._heartbeat = replace(self._heartbeat, state="error", safe_error_code=safe.code)
            await self.close(preserve_error=True)
            raise safe from None

    async def close(self, *, preserve_error: bool = False) -> None:
        socket, self._socket = self._socket, None
        self._subscriptions.clear()
        self._pending.clear()
        if socket is not None:
            try:
                await asyncio.wait_for(socket.close(), self._handshake_timeout)
            except Exception:
                pass
        if self._owned_rpc is not None:
            try:
                await asyncio.wait_for(self._owned_rpc.close(), self._handshake_timeout)
            except Exception:
                pass
            finally:
                self._owned_rpc = None
                self._rpc = None
        if not preserve_error:
            self._heartbeat = replace(self._heartbeat, state="disconnected")
