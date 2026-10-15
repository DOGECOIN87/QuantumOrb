"""Offline end-to-end coverage and pagination checks for wallet history."""

from __future__ import annotations

import asyncio
import copy
import json
from collections import deque
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.config import BASE58_ALPHABET, Settings
from app.db.models import job_runs, raw_events
from app.db.session import create_all, create_database_engine, create_session_factory
from app.integrations import wallet_history as history_module
from app.integrations.helius_http import HeliusHttpClient, HeliusHttpError
from app.integrations.wallet_history import WalletHistoryAdapter
from app.integrations.wallet_history_models import HistoryQuery

NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)
WALLET = "1" * 32
API_KEY = "synthetic-wallet-history-test-secret"


def live_settings(**overrides):
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


def query(**overrides):
    return HistoryQuery(
        **{
            "wallet": WALLET,
            "start_time": NOW - timedelta(minutes=10),
            "end_time": NOW,
            "page_size": 2,
            **overrides,
        }
    )


def signature(number):
    data = number.to_bytes(64, "big")
    encoded = ""
    while number:
        number, digit = divmod(number, 58)
        encoded = BASE58_ALPHABET[digit] + encoded
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + encoded


def transaction(number=1, *, slot=None, index=0, age=60, error=None, **updates):
    return {
        "slot": 300000000 + number if slot is None else slot,
        "transactionIndex": index,
        "blockTime": int(NOW.timestamp()) - age,
        "version": "legacy",
        "transaction": {
            "signatures": [signature(number)],
            "message": {"accountKeys": [WALLET], "instructions": []},
        },
        "meta": {
            "err": error,
            "fee": 5000,
            "preBalances": [10000],
            "postBalances": [5000],
            "preTokenBalances": [],
            "postTokenBalances": [],
            "innerInstructions": [],
            "logMessages": [],
        },
        **updates,
    }


def page(*transactions, token=None):
    return {"result": {"data": list(transactions), "paginationToken": token}}


@pytest.fixture
def factory(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'wallet-history.db'}")
    create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def stored(factory, table=raw_events):
    with factory() as session:
        return list(session.execute(select(table)).mappings())


class Rpc:
    def __init__(self, *responses):
        self.responses = deque(responses)
        self.calls = []
        self.closed = False
        self.before_call = None

    async def rpc(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if self.before_call:
            self.before_call(method, params)
        value = self.responses.popleft()
        if isinstance(value, BaseException):
            raise value
        return value

    async def close(self):
        self.closed = True


def fetch(factory, rpc, *, requested=None, settings=None, **options):
    adapter = WalletHistoryAdapter(
        settings or live_settings(), factory, rpc=rpc, clock=lambda: NOW, **options
    )
    return asyncio.run(adapter.fetch(requested or query()))


def test_pages_are_fetched_until_explicit_null_and_options_preserve_filters(factory):
    rpc = Rpc(page(transaction(1), token="300000001:0"), page(transaction(2)))
    result = fetch(factory, rpc)
    assert result.coverage.history_complete and result.coverage.exhausted
    assert result.coverage.pages_fetched == 2
    assert result.coverage.transactions_seen == result.coverage.transactions_returned == 2
    assert result.coverage.next_pagination_token is None
    assert [item.signature for item in result.transactions] == [signature(1), signature(2)]
    assert all(method == "getTransactionsForAddress" for method, _ in rpc.calls)
    first = rpc.calls[0][1]
    assert first == [
        WALLET,
        {
            "transactionDetails": "full",
            "encoding": "json",
            "maxSupportedTransactionVersion": 1,
            "sortOrder": "asc",
            "commitment": "finalized",
            "limit": 2,
            "filters": {
                "blockTime": {
                    "gte": int((NOW - timedelta(minutes=10)).timestamp()),
                    "lt": int(NOW.timestamp()),
                },
                "status": "succeeded",
                "tokenAccounts": "balanceChanged",
            },
        },
    ]
    assert rpc.calls[1][1][1]["paginationToken"] == "300000001:0"
    assert not rpc.closed


def test_short_and_empty_pages_with_continuation_are_not_exhaustion(factory):
    rpc = Rpc(page(token="300000001:0"), page(transaction(1), token="300000002:0"), page())
    result = fetch(factory, rpc)
    assert result.coverage.history_complete and result.coverage.exhausted
    assert result.coverage.pages_fetched == 3 and len(result.transactions) == 1


def test_empty_explicit_terminal_page_is_complete_filtered_history(factory):
    result = fetch(factory, Rpc(page()))
    assert result.coverage.history_complete and result.coverage.exhausted
    assert result.transactions == () and result.coverage.transactions_seen == 0
    assert len(result.page_raw_event_ids) == 1


def test_exact_limit_with_null_token_is_complete(factory):
    result = fetch(
        factory, Rpc(page(transaction(1), transaction(2))), requested=query(max_transactions=2)
    )
    assert result.coverage.history_complete and result.coverage.exhausted
    assert result.coverage.transactions_seen == len(result.transactions) == 2


def test_exact_limit_with_more_history_is_explicitly_incomplete(factory):
    rpc = Rpc(page(transaction(1), transaction(2), token="300000002:0"))
    result = fetch(factory, rpc, requested=query(max_transactions=2))
    assert not result.coverage.history_complete and not result.coverage.exhausted
    assert "history_limit_reached" in result.coverage.incomplete_reasons
    assert result.coverage.next_pagination_token == "300000002:0"
    assert len(rpc.calls) == 1


def test_page_limit_bounds_empty_advancing_pages(factory):
    rpc = Rpc(page(token="300000001:0"), page(token="300000002:0"))
    result = fetch(factory, rpc, max_pages=2)
    assert not result.coverage.history_complete and not result.coverage.exhausted
    assert "history_page_limit_reached" in result.coverage.incomplete_reasons
    assert result.coverage.next_pagination_token == "300000002:0"
    assert len(rpc.calls) == 2


@pytest.mark.parametrize(
    "responses",
    [
        [page(token="300000001:0"), page(token="300000001:0")],
        [page(token="300000001:0"), page(token="300000002:0"), page(token="300000001:0")],
    ],
)
def test_repeated_or_cyclic_tokens_terminate_with_gap(factory, responses):
    rpc = Rpc(*responses)
    result = fetch(factory, rpc)
    assert not result.coverage.history_complete and not result.coverage.exhausted
    assert "history_pagination_loop" in result.coverage.incomplete_reasons
    assert len(rpc.calls) == len(responses)


def test_resumed_suffix_is_never_presented_as_complete_window(factory):
    requested = query(pagination_token="300000001:0")
    rpc = Rpc(page(transaction(2)))
    result = fetch(factory, rpc, requested=requested)
    assert result.coverage.exhausted and not result.coverage.history_complete
    assert "history_resumed" in result.coverage.incomplete_reasons
    assert rpc.calls[0][1][1]["paginationToken"] == requested.pagination_token


def test_identical_overlap_is_deduplicated_but_counts_toward_raw_entry_budget(factory):
    rpc = Rpc(
        page(transaction(1), transaction(2), token="300000002:0"),
        page(transaction(2), token="300000003:0"),
    )
    result = fetch(factory, rpc, requested=query(max_transactions=3))
    assert result.coverage.transactions_seen == 3 and result.coverage.transactions_returned == 2
    assert len(result.transactions) == 2 and not result.coverage.history_complete
    assert rpc.calls[1][1][1]["limit"] == 1
    assert "history_limit_reached" in result.coverage.incomplete_reasons


def test_overlap_before_new_unique_preserves_chronology(factory):
    rpc = Rpc(
        page(transaction(1), transaction(2), token="300000002:0"),
        page(transaction(2), transaction(3)),
    )
    result = fetch(factory, rpc)
    assert result.coverage.history_complete
    assert [item.signature for item in result.transactions] == [
        signature(1),
        signature(2),
        signature(3),
    ]
    assert result.coverage.transactions_seen == 4


def test_conflicting_duplicate_is_gap_with_request_token_preserved(factory):
    conflict = transaction(1)
    conflict["meta"]["fee"] = 4000
    rpc = Rpc(page(transaction(1), token="300000001:0"), page(conflict, token="300000002:0"))
    result = fetch(factory, rpc)
    assert not result.coverage.history_complete
    assert "history_transaction_conflict" in result.coverage.incomplete_reasons
    assert result.coverage.next_pagination_token == "300000001:0"
    assert [item.signature for item in result.transactions] == [signature(1)]


@pytest.mark.parametrize(
    "transactions",
    [
        [transaction(2), transaction(1)],
        [transaction(1, slot=300000001, index=2), transaction(2, slot=300000001, index=1)],
    ],
)
def test_chronology_uses_slot_then_transaction_index_not_signature(factory, transactions):
    result = fetch(factory, Rpc(page(*transactions, token="300000003:0")))
    assert not result.coverage.history_complete
    assert "history_order_invalid" in result.coverage.incomplete_reasons
    assert result.coverage.next_pagination_token is None


def test_same_slot_order_uses_transaction_index(factory):
    rpc = Rpc(
        page(transaction(9, slot=300000001, index=1), transaction(1, slot=300000001, index=2))
    )
    result = fetch(factory, rpc)
    assert result.coverage.history_complete
    assert [item.signature for item in result.transactions] == [signature(9), signature(1)]


def test_descending_order_is_preserved_when_explicitly_requested(factory):
    rpc = Rpc(page(transaction(2), transaction(1)))
    result = fetch(factory, rpc, requested=query(sort_order="desc"))
    assert result.coverage.history_complete
    assert [item.slot for item in result.transactions] == [300000002, 300000001]
    assert rpc.calls[0][1][1]["sortOrder"] == "desc"


@pytest.mark.parametrize(
    "body",
    [
        transaction(blockTime=None),
        transaction(age=601),
        transaction(age=0),
        transaction(error="AccountNotFound"),
    ],
)
def test_unknown_time_or_provider_filter_mismatch_cannot_claim_complete_history(factory, body):
    result = fetch(factory, Rpc(page(body, token="300000002:0")))
    assert not result.coverage.history_complete and result.coverage.incomplete_reasons
    assert result.transactions == ()
    assert result.coverage.next_pagination_token is None


@pytest.mark.parametrize(
    "status,error",
    [
        ("failed", {"InstructionError": [0, "Custom"]}),
        ("any", "AccountNotFound"),
        ("any", None),
    ],
)
def test_explicit_status_filter_accepts_matching_metadata(factory, status, error):
    result = fetch(factory, Rpc(page(transaction(error=error))), requested=query(status=status))
    assert result.coverage.history_complete and len(result.transactions) == 1
    assert result.transactions[0].transaction_error == error


@pytest.mark.parametrize(
    "response",
    [
        None,
        {"result": None},
        {"result": []},
        {"result": {"data": []}},
        {"result": {"data": [], "paginationToken": 42}},
        {"result": {"data": [], "paginationToken": None, "future": True}},
    ],
)
def test_malformed_response_is_retained_and_does_not_advance_token(factory, response):
    rpc = Rpc(page(transaction(1), token="300000001:0"), response)
    result = fetch(factory, rpc)
    assert not result.coverage.history_complete and result.coverage.incomplete_reasons
    assert not result.coverage.exhausted
    assert result.coverage.next_pagination_token == "300000001:0"
    assert [item.signature for item in result.transactions] == [signature(1)]
    assert len(result.page_raw_event_ids) == 2
    page_rows = [row for row in stored(factory) if row["id"] in result.page_raw_event_ids]
    assert len(page_rows) == 2
    assert any(json.loads(row["payload_json"]).get("response") == response for row in page_rows)


def test_invalid_transaction_is_retained_before_type_validation(factory):
    body = transaction(transactionIndex=True)
    response = page(body, token="300000001:0")
    result = fetch(factory, Rpc(response))
    assert not result.coverage.history_complete and not result.transactions
    assert result.coverage.next_pagination_token is None
    assert any(json.loads(row["payload_json"]) == body for row in stored(factory))
    assert any(
        json.loads(row["payload_json"]).get("response") == response for row in stored(factory)
    )


def test_previous_page_is_visible_before_next_request(factory):
    response = page(transaction(1), token="300000001:0")
    rpc = Rpc(response, page(transaction(2)))

    def observer(method, params):
        if params[1].get("paginationToken"):
            assert any(
                json.loads(row["payload_json"]).get("response") == response
                for row in stored(factory)
            )
            assert any(json.loads(row["payload_json"]) == transaction(1) for row in stored(factory))

    rpc.before_call = observer
    assert fetch(factory, rpc).coverage.history_complete


def test_returned_typed_rows_reference_previously_persisted_bare_transactions(factory):
    result = fetch(factory, Rpc(page(transaction(1))))
    saved = {row["id"]: row for row in stored(factory)}
    body = result.transactions[0]
    assert body.raw_event_id in saved and body.page_raw_event_id in result.page_raw_event_ids
    assert json.loads(saved[body.raw_event_id]["payload_json"]) == transaction(1)
    assert body.payload_json == saved[body.raw_event_id]["payload_json"]
    assert saved[body.raw_event_id]["transport"] == "helius_rpc"


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError(API_KEY),
        HeliusHttpError("helius_unauthorized", API_KEY, status_code=401),
        HeliusHttpError("helius_rpc_error", API_KEY, retryable=False),
    ],
)
def test_rpc_failure_returns_partial_safe_result_preserving_prior_page(factory, error):
    rpc = Rpc(page(transaction(1), token="300000001:0"), error)
    result = fetch(factory, rpc)
    assert not result.coverage.history_complete and not result.coverage.exhausted
    assert result.coverage.next_pagination_token == "300000001:0"
    assert [item.signature for item in result.transactions] == [signature(1)]
    assert API_KEY not in repr(result.coverage)
    assert API_KEY not in json.dumps([dict(row) for row in stored(factory, job_runs)], default=str)


def test_timeout_is_cancelled_and_returned_as_incomplete_coverage(factory):
    class SlowRpc(Rpc):
        async def rpc(self, method, params):
            await asyncio.sleep(60)

    result = fetch(factory, SlowRpc(), rpc_timeout=0.01)
    assert not result.coverage.history_complete and result.coverage.incomplete_reasons
    assert not result.coverage.exhausted and result.transactions == ()


def test_replay_reuses_raw_rows_but_records_a_distinct_immutable_audit(factory):
    response = page(transaction(1))
    first = fetch(factory, Rpc(response))
    raw_count = len(stored(factory))
    original_audit = dict(stored(factory, job_runs)[0])
    second = fetch(factory, Rpc(response))
    assert len(stored(factory)) == raw_count
    assert first.page_raw_event_ids == second.page_raw_event_ids
    assert first.transactions[0].raw_event_id == second.transactions[0].raw_event_id
    assert first.run_id != second.run_id
    audits = {row["id"]: dict(row) for row in stored(factory, job_runs)}
    assert audits[first.run_id] == original_audit
    assert audits[second.run_id]["status"] == "success"
    counts = json.loads(audits[second.run_id]["counts_json"])
    assert "coverage" in counts and counts["coverage"]["history_complete"] is True
    assert API_KEY not in repr(counts)


def test_cancellation_closes_run_audit_and_propagates(factory):
    rpc = Rpc(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        fetch(factory, rpc)
    audit = stored(factory, job_runs)[0]
    assert audit["status"] != "running" and audit["completed_at"] is not None
    assert audit["safe_error_code"] and API_KEY not in str(audit["safe_error_message"])


def test_default_http_client_uses_header_auth_and_only_wallet_history_rpc(factory, monkeypatch):
    calls = []

    def handler(request):
        assert request.headers["x-api-key"] == API_KEY
        assert API_KEY not in str(request.url)
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], **page(transaction())})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = HeliusHttpClient(API_KEY, "https://rpc.example.invalid/", client=http_client)
    constructed = []
    closed = []
    original_close = client.close

    async def close():
        closed.append(True)
        await original_close()

    monkeypatch.setattr(client, "close", close)

    def build(*args, **kwargs):
        constructed.append((args, kwargs))
        return client

    monkeypatch.setattr(history_module, "HeliusHttpClient", build)
    try:
        adapter = WalletHistoryAdapter(live_settings(), factory, clock=lambda: NOW)
        assert constructed == []
        result = asyncio.run(adapter.fetch(query()))
    finally:
        asyncio.run(http_client.aclose())
    assert result.coverage.history_complete and len(result.transactions) == 1
    assert len(constructed) == len(calls) == 1
    assert closed == [True]
    assert calls[0]["method"] == "getTransactionsForAddress"
    assert all(API_KEY not in row["payload_json"] for row in stored(factory))


def test_typed_parsers_observe_committed_page_and_bare_transaction(factory, monkeypatch):
    response = page(transaction())
    original_page = history_module.parse_history_page
    original_transaction = history_module.parse_history_transaction
    inspected = []

    def parse_page(value, **kwargs):
        assert any(
            json.loads(row["payload_json"]).get("response") == value for row in stored(factory)
        )
        inspected.append("page")
        return original_page(value, **kwargs)

    def parse_transaction(value, **kwargs):
        saved = {row["id"]: row for row in stored(factory)}
        assert json.loads(saved[kwargs["raw_event_id"]]["payload_json"]) == value
        assert kwargs["page_raw_event_id"] in saved
        inspected.append("transaction")
        return original_transaction(value, **kwargs)

    monkeypatch.setattr(history_module, "parse_history_page", parse_page)
    monkeypatch.setattr(history_module, "parse_history_transaction", parse_transaction)
    result = fetch(factory, Rpc(response))
    assert result.coverage.history_complete and inspected == ["page", "transaction"]


def test_explicit_confirmed_token_account_and_failure_filters_are_sent(factory):
    rpc = Rpc(page(transaction(error="AccountNotFound")))
    requested = query(commitment="confirmed", token_accounts="all", status="failed")
    result = fetch(factory, rpc, requested=requested)
    assert len(result.transactions) == 1
    options = rpc.calls[0][1][1]
    assert options["commitment"] == "confirmed"
    assert options["filters"]["status"] == "failed"
    assert options["filters"]["tokenAccounts"] == "all"
    assert all(row["commitment"] == "confirmed" for row in stored(factory))


@pytest.mark.parametrize(
    "updates,requested",
    [
        ({"cartographer_max_history_tx": 1}, query(max_transactions=2)),
        ({"backfill_page_size": 1}, query(page_size=2)),
        ({"cartographer_lookback_days": 1}, query(start_time=NOW - timedelta(days=2))),
        ({}, query(end_time=NOW + timedelta(seconds=1))),
        ({"ingest_mode": "demo", "ingest_transport": "fixture"}, query()),
        ({"solana_network": "devnet"}, query()),
    ],
)
def test_configured_bounds_and_live_mainnet_mode_are_required_before_rpc(
    factory, updates, requested
):
    rpc = Rpc()
    with pytest.raises(history_module.WalletHistoryError) as raised:
        fetch(factory, rpc, settings=live_settings(**updates), requested=requested)
    assert raised.value.code and API_KEY not in str(raised.value)
    assert rpc.calls == []


def test_adapter_does_not_start_network_calls_during_construction(factory):
    rpc = Rpc()
    WalletHistoryAdapter(live_settings(), factory, rpc=rpc, clock=lambda: NOW)
    assert rpc.calls == [] and stored(factory) == [] and stored(factory, job_runs) == []


def test_historical_token_account_coverage_limit_is_explicit(factory):
    start = datetime(2022, 11, 1, tzinfo=UTC)
    requested = query(start_time=start, end_time=start + timedelta(days=1))
    result = fetch(factory, Rpc(page()), requested=requested)
    assert result.coverage.exhausted and not result.coverage.history_complete
    assert "token_account_history_unavailable" in result.coverage.incomplete_reasons
    assert result.coverage.warnings


def test_clock_requires_timezone_before_rpc(factory):
    rpc = Rpc()
    adapter = WalletHistoryAdapter(
        live_settings(), factory, rpc=rpc, clock=lambda: NOW.replace(tzinfo=None)
    )
    with pytest.raises(history_module.WalletHistoryError):
        asyncio.run(adapter.fetch(query()))
    assert rpc.calls == []


@pytest.mark.parametrize(
    "option,value",
    [
        ("rpc_timeout", 0),
        ("rpc_timeout", True),
        ("rpc_timeout", float("nan")),
        ("rpc_timeout", float("inf")),
        ("rpc_timeout", "1"),
        ("max_pages", 0),
        ("max_pages", 101),
        ("max_pages", True),
    ],
)
def test_runtime_resource_bounds_are_validated_before_network(factory, option, value):
    rpc = Rpc()
    with pytest.raises(history_module.WalletHistoryError):
        WalletHistoryAdapter(live_settings(), factory, rpc=rpc, **{option: value})
    assert rpc.calls == []


def test_modified_unvalidated_query_is_revalidated_without_exposing_values(factory):
    requested = query().model_copy(update={"wallet": API_KEY})
    rpc = Rpc()
    with pytest.raises(history_module.WalletHistoryError) as raised:
        fetch(factory, rpc, requested=requested)
    assert API_KEY not in str(raised.value) and rpc.calls == []


@pytest.mark.parametrize("invalid", [NOW.replace(tzinfo=None), RuntimeError(API_KEY)])
def test_second_clock_failure_is_safe_and_does_not_lock_later_fetch(factory, invalid):
    calls = 0

    def clock():
        nonlocal calls
        calls += 1
        if calls == 2:
            if isinstance(invalid, BaseException):
                raise invalid
            return invalid
        return NOW

    rpc = Rpc(page(transaction()))
    adapter = WalletHistoryAdapter(live_settings(), factory, rpc=rpc, clock=clock)
    with pytest.raises(history_module.WalletHistoryError) as raised:
        asyncio.run(adapter.fetch(query()))
    assert raised.value.code == "invalid_wallet_history_clock"
    assert API_KEY not in str(raised.value) and rpc.calls == []
    assert stored(factory, job_runs) == []
    result = asyncio.run(adapter.fetch(query()))
    assert result.coverage.history_complete and len(rpc.calls) == 1


def test_resumed_query_rpc_error_is_the_primary_audit_error(factory):
    requested = query(pagination_token="300000001:0")
    result = fetch(factory, Rpc(RuntimeError(API_KEY)), requested=requested)
    assert "history_resumed" in result.coverage.incomplete_reasons
    assert "wallet_history_rpc_failed" in result.coverage.incomplete_reasons
    assert result.coverage.safe_error_code == "wallet_history_rpc_failed"
    audit = stored(factory, job_runs)[0]
    assert audit["safe_error_code"] == "wallet_history_rpc_failed"
    assert API_KEY not in json.dumps(dict(audit), default=str)


def test_direct_wallet_filter_rejects_transaction_without_wallet_reference(factory):
    body = transaction()
    body["transaction"]["message"]["accountKeys"] = ["So11111111111111111111111111111111111111112"]
    result = fetch(factory, Rpc(page(body)), requested=query(token_accounts="none"))
    assert not result.coverage.history_complete and not result.transactions
    assert "history_filter_mismatch" in result.coverage.incomplete_reasons


@pytest.mark.parametrize("loaded_kind", ["writable", "readonly"])
def test_direct_wallet_filter_accepts_resolved_lookup_reference(factory, loaded_kind):
    other = "So11111111111111111111111111111111111111112"
    body = transaction(version=0)
    body["transaction"]["message"]["accountKeys"] = [other]
    body["transaction"]["message"]["addressTableLookups"] = [
        {
            "accountKey": other,
            "writableIndexes": [0] if loaded_kind == "writable" else [],
            "readonlyIndexes": [0] if loaded_kind == "readonly" else [],
        }
    ]
    body["meta"]["loadedAddresses"] = {
        "writable": [WALLET] if loaded_kind == "writable" else [],
        "readonly": [WALLET] if loaded_kind == "readonly" else [],
    }
    body["meta"]["preBalances"] = [10000, 0]
    body["meta"]["postBalances"] = [5000, 0]
    result = fetch(factory, Rpc(page(body)), requested=query(token_accounts="none"))
    assert result.coverage.history_complete and len(result.transactions) == 1
