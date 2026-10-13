"""Safe HTTP transport primitives for Helius JSON-RPC and REST calls."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, TypeVar
from uuid import uuid4

import httpx
from pydantic import SecretStr

from app.logging_config import SafeError, log_event

Json = dict[str, Any] | list[Any]
T = TypeVar("T", bound=Json)

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_LOGGER = logging.getLogger(__name__)


class HeliusHttpError(SafeError):
    """Safe provider error that never includes response bodies or credentials."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(code, message, retryable=retryable)
        self.status_code = status_code


class HeliusHttpClient:
    """Async Helius client with bounded retries and a simple request limiter."""

    def __init__(
        self,
        api_key: SecretStr | str,
        base_url: str,
        *,
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
        backoff_base_seconds: float = 0.25,
        backoff_max_seconds: float = 8.0,
        requests_per_second: float = 10.0,
        client: httpx.AsyncClient | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        secret = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        if not secret or not secret.strip():
            raise ValueError("Helius API key is required")
        if not base_url.startswith(("https://", "http://")) or "?" in base_url or "#" in base_url:
            raise ValueError("Helius base URL must be an HTTP(S) URL without query or fragment")
        if max_retries < 0 or max_retries > 5:
            raise ValueError("max_retries must be between 0 and 5")
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self._api_key = secret
        self._base_url = base_url.rstrip("/") + "/"
        self._timeout = httpx.Timeout(timeout_seconds)
        self._max_retries = max_retries
        self._backoff_base = backoff_base_seconds
        self._backoff_max = backoff_max_seconds
        self._interval = 1.0 / requests_per_second
        self._client = client
        self._owns_client = client is None
        self._sleeper = sleeper
        self._jitter = jitter
        self._rate_lock = asyncio.Lock()
        self._next_request_at = 0.0

    async def __aenter__(self) -> HeliusHttpClient:
        await self._ensure_client()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.close()

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def _wait_for_rate_limit(self) -> None:
        async with self._rate_lock:
            now = time.monotonic()
            wait = max(0.0, self._next_request_at - now)
            self._next_request_at = max(now, self._next_request_at) + self._interval
        if wait:
            await self._sleeper(wait)

    def _url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            raise HeliusHttpError("invalid_endpoint", "absolute provider endpoints are not allowed")
        return self._base_url + path.lstrip("/")

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int | bool] | None = None,
        json_body: Json | None = None,
    ) -> Json:
        """Perform a JSON request, retrying only transient provider failures."""
        if params and any("key" in name.lower() or "token" in name.lower() for name in params):
            raise HeliusHttpError(
                "unsafe_query_parameter", "credentials must not be placed in query parameters"
            )
        client = await self._ensure_client()
        url = self._url(path)
        headers = {"accept": "application/json", "x-api-key": self._api_key}
        for attempt in range(self._max_retries + 1):
            await self._wait_for_rate_limit()
            try:
                response = await client.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=headers,
                    timeout=self._timeout,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as error:
                if attempt >= self._max_retries:
                    raise HeliusHttpError.from_exception(
                        "helius_transport_error",
                        error,
                        message="provider transport failed",
                        retryable=True,
                    ) from None
                log_event(
                    _LOGGER,
                    logging.WARNING,
                    "helius request retry",
                    fields={"attempt": attempt + 1, "reason": "transport"},
                )
                await self._backoff(attempt)
                continue
            if response.status_code in _RETRYABLE_STATUS and attempt < self._max_retries:
                log_event(
                    _LOGGER,
                    logging.WARNING,
                    "helius request retry",
                    fields={"attempt": attempt + 1, "status_code": response.status_code},
                )
                await self._backoff(attempt, retry_after=response.headers.get("retry-after"))
                continue
            if response.status_code >= 400:
                code = (
                    "helius_unauthorized"
                    if response.status_code in {401, 403}
                    else "helius_http_error"
                )
                raise HeliusHttpError(
                    code,
                    "provider request rejected",
                    status_code=response.status_code,
                    retryable=response.status_code in _RETRYABLE_STATUS,
                )
            try:
                decoded = response.json()
            except ValueError:
                raise HeliusHttpError(
                    "helius_invalid_json", "provider returned invalid JSON"
                ) from None
            if not isinstance(decoded, (dict, list)):
                raise HeliusHttpError(
                    "helius_invalid_json", "provider returned an unsupported JSON value"
                )
            return decoded
        raise HeliusHttpError(
            "helius_retry_exhausted", "provider retry budget exhausted", retryable=True
        )

    async def _backoff(self, attempt: int, *, retry_after: str | None = None) -> None:
        delay = self._backoff_base * (2**attempt)
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        delay = min(delay, self._backoff_max)
        await self._sleeper(self._jitter(0.5 * delay, 1.5 * delay))

    async def rpc(
        self, method: str, params: list[Any] | dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Call a JSON-RPC method and convert provider errors to safe errors."""
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": str(uuid4()), "method": method}
        if params is not None:
            payload["params"] = params
        response = await self.request_json("POST", "", json_body=payload)
        if not isinstance(response, dict):
            raise HeliusHttpError(
                "helius_invalid_rpc_response", "provider returned an invalid RPC response"
            )
        if "error" in response:
            raise HeliusHttpError("helius_rpc_error", "provider returned an RPC error")
        if "result" not in response:
            raise HeliusHttpError(
                "helius_invalid_rpc_response", "provider RPC result was not JSON data"
            )
        return {"result": response["result"]}

    async def get_json(
        self, path: str, *, params: Mapping[str, str | int | bool] | None = None
    ) -> Json:
        return await self.request_json("GET", path, params=params)
