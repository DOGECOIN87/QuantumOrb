import asyncio
import json

import httpx
import pytest

from app.integrations.helius_http import HeliusHttpClient, HeliusHttpError

API_KEY = "helius-fixture-secret"


def run(coro):
    return asyncio.run(coro)


def make_client(handler, *, max_retries=2):
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    sleeps: list[float] = []

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    client = HeliusHttpClient(
        API_KEY,
        "https://rpc.example.invalid/",
        client=http_client,
        max_retries=max_retries,
        backoff_base_seconds=0.01,
        sleeper=sleeper,
        jitter=lambda low, high: low,
        requests_per_second=100000,
    )
    return client, sleeps, http_client


def test_200_json_rpc_response_and_header_are_typed_and_secret_safe():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == API_KEY
        assert API_KEY not in str(request.url)
        body = json.loads(request.content)
        assert body["method"] == "getSlot"
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": 123})

    client, _, http_client = make_client(handler)
    try:
        result = run(client.rpc("getSlot"))
    finally:
        run(http_client.aclose())
    assert result == {"result": 123}


def test_401_is_not_retried_and_has_safe_error():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401, json={"secret": API_KEY})

    client, sleeps, http_client = make_client(handler)
    try:
        with pytest.raises(HeliusHttpError) as raised:
            run(client.get_json("v0/transactions"))
    finally:
        run(http_client.aclose())
    assert attempts == 1
    assert sleeps == []
    assert raised.value.code == "helius_unauthorized"
    assert API_KEY not in str(raised.value)


def test_429_and_500_are_retried_with_bounded_jitter():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(429 if attempts == 1 else 500, headers={"retry-after": "0"})
        return httpx.Response(200, json={"ok": True})

    client, sleeps, http_client = make_client(handler, max_retries=2)
    try:
        result = run(client.get_json("v0/transactions"))
    finally:
        run(http_client.aclose())
    assert result == {"ok": True}
    assert attempts == 3
    assert len(sleeps) == 2
    assert sleeps[1] >= sleeps[0]


def test_timeout_retries_then_returns_safe_transport_error():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("fixture timeout", request=request)

    client, sleeps, http_client = make_client(handler, max_retries=1)
    try:
        with pytest.raises(HeliusHttpError) as raised:
            run(client.get_json("v0/transactions"))
    finally:
        run(http_client.aclose())
    assert attempts == 2
    assert len(sleeps) == 1
    assert raised.value.code == "helius_transport_error"
    assert "fixture timeout" not in str(raised.value)


def test_credentials_are_rejected_in_query_parameters():
    client, _, http_client = make_client(lambda request: httpx.Response(200, json={}))
    try:
        with pytest.raises(HeliusHttpError, match="credentials must not"):
            run(client.get_json("v0/transactions", params={"api-key": API_KEY}))
    finally:
        run(http_client.aclose())


@pytest.mark.parametrize(
    "content",
    [
        b'{"meta":{"err":{"failure":1},"err":null}}',
        b'{"amount":NaN}',
        b'{"amount":Infinity}',
        b'{"amount":1e9999}',
        b'"scalar"',
        b"\xff",
        b"{invalid}",
    ],
)
def test_ambiguous_or_nonfinite_json_is_rejected_without_retry(content):
    client, sleeps, http_client = make_client(lambda request: httpx.Response(200, content=content))
    try:
        with pytest.raises(HeliusHttpError) as raised:
            run(client.get_json(""))
    finally:
        run(http_client.aclose())
    assert raised.value.code == "helius_invalid_json"
    assert not raised.value.retryable
    assert sleeps == []


@pytest.mark.parametrize("change", ["id", "version", "extra", "both", "missing"])
def test_rpc_response_must_match_the_request_envelope(change):
    def handler(request):
        body = json.loads(request.content)
        response = {"jsonrpc": "2.0", "id": body["id"], "result": None}
        if change == "id":
            response["id"] = "different-request"
        elif change == "version":
            response["jsonrpc"] = "1.0"
        elif change == "extra":
            response["future"] = API_KEY
        elif change == "both":
            response["error"] = {"code": -32001, "message": API_KEY}
        else:
            del response["result"]
        return httpx.Response(200, json=response)

    client, sleeps, http_client = make_client(handler)
    try:
        with pytest.raises(HeliusHttpError) as raised:
            run(client.rpc("getTransaction"))
    finally:
        run(http_client.aclose())
    assert raised.value.code == "helius_invalid_rpc_response"
    assert not raised.value.retryable
    assert API_KEY not in str(raised.value)
    assert sleeps == []


@pytest.mark.parametrize("rpc_code,retryable", [(-32001, True), (-32002, True), (-32602, False)])
def test_rpc_error_keeps_safe_retry_classification(rpc_code, retryable):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": json.loads(request.content)["id"],
                "error": {"code": rpc_code, "message": API_KEY},
            },
        )

    client, _, http_client = make_client(handler)
    try:
        with pytest.raises(HeliusHttpError) as raised:
            run(client.rpc("getSlot"))
    finally:
        run(http_client.aclose())
    assert raised.value.code == "helius_rpc_error"
    assert raised.value.retryable is retryable
    assert API_KEY not in str(raised.value)


def test_http_408_is_retried_as_a_transient_timeout():
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(408 if attempts == 1 else 200, json={"ok": True})

    client, sleeps, http_client = make_client(handler)
    try:
        assert run(client.get_json("")) == {"ok": True}
    finally:
        run(http_client.aclose())
    assert attempts == 2
    assert len(sleeps) == 1
