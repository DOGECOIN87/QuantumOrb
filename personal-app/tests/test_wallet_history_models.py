"""Typed full-history boundary and exact RPC filters without live provider access."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.integrations.wallet_history_models import (
    HistoryQuery,
    HistoryTransaction,
    HistoryValidationError,
    parse_history_page,
    parse_history_transaction,
)

NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)
WALLET = "1" * 32
SIGNATURE = "1" * 64
MAX_U64 = (1 << 64) - 1


def query(**changes):
    return HistoryQuery(
        **{"wallet": WALLET, "start_time": NOW - timedelta(days=1), "end_time": NOW, **changes}
    )


def transaction():
    return {
        "slot": 300_000_001,
        "transactionIndex": 4,
        "blockTime": int(NOW.timestamp()),
        "transaction": {
            "signatures": [SIGNATURE],
            "message": {
                "accountKeys": [WALLET],
                "instructions": [{"programIdIndex": 0, "accounts": [], "data": ""}],
            },
        },
        "meta": {"err": None, "fee": 5000, "preBalances": [10000], "postBalances": [5000]},
    }


def parse(payload):
    return parse_history_transaction(payload, raw_event_id=7, page_raw_event_id=3)


def test_query_builds_exact_scoped_full_json_request_with_opaque_token():
    token = "opaque continuation:position/is-not-derived"
    request = query()
    assert request.rpc_params(75, token) == [
        WALLET,
        {
            "transactionDetails": "full",
            "encoding": "json",
            "maxSupportedTransactionVersion": 1,
            "sortOrder": "asc",
            "commitment": "finalized",
            "limit": 75,
            "paginationToken": token,
            "filters": {
                "status": "succeeded",
                "tokenAccounts": "balanceChanged",
                "blockTime": {
                    "gte": int((NOW - timedelta(days=1)).timestamp()),
                    "lt": int(NOW.timestamp()),
                },
            },
        },
    ]
    assert "paginationToken" not in request.rpc_params(1, None)[1]
    assert request.pagination_token is None
    with pytest.raises(ValidationError):
        request.wallet = "replacement"


def test_nondefault_filters_are_typed_and_aware_offsets_normalize_to_utc():
    offset = timezone(timedelta(hours=-5))
    request = query(
        start_time=(NOW - timedelta(days=30)).astimezone(offset),
        end_time=NOW.astimezone(offset),
        sort_order="desc",
        status="failed",
        token_accounts="all",
        commitment="confirmed",
        page_size=1000,
        max_transactions=1,
    )
    assert request.start_time.tzinfo is UTC and request.end_time == NOW
    options = request.rpc_params(1000, None)[1]
    assert options["sortOrder"] == "desc" and options["commitment"] == "confirmed"
    assert options["filters"]["status"] == "failed"
    assert options["filters"]["tokenAccounts"] == "all"


@pytest.mark.parametrize("field", ["page_size", "max_transactions"])
@pytest.mark.parametrize("value", [0, -1, 1001, True, 1.0, "100"])
def test_query_limits_are_strict_positive_bounded_integers(field, value):
    with pytest.raises(ValidationError):
        query(**{field: value})


@pytest.mark.parametrize(
    "changes",
    [
        {"wallet": "invalid-address"},
        {"wallet": 111},
        {"start_time": NOW},
        {"start_time": NOW + timedelta(seconds=1)},
        {"start_time": NOW - timedelta(days=30, seconds=1)},
        {"start_time": datetime(1969, 12, 31, tzinfo=UTC)},
        {"start_time": NOW.replace(tzinfo=None)},
        {"end_time": NOW.replace(microsecond=1)},
        {"end_time": NOW.replace(tzinfo=timezone(timedelta(microseconds=500000)))},
        {"start_time": int(NOW.timestamp())},
        {"start_time": NOW.timestamp()},
        {"start_time": "2026-10-12T12:00:00Z"},
        {"sort_order": "chronological"},
        {"status": "ok"},
        {"token_accounts": "balance_changed"},
        {"commitment": "processed"},
        {"unexpected": True},
    ],
)
def test_invalid_query_scope_does_not_become_an_rpc_request(changes):
    with pytest.raises(ValidationError):
        query(**changes)


@pytest.mark.parametrize("token", ["", "x" * 1025, "next\npage", "next\rpage", "\0", "\u200b", 10])
def test_pagination_tokens_are_printable_bounded_opaque_strings(token):
    with pytest.raises(ValidationError):
        query(pagination_token=token)
    with pytest.raises(HistoryValidationError) as raised:
        query().rpc_params(100, token)
    assert raised.value.code == "invalid_history_pagination"


@pytest.mark.parametrize("limit", [False, 0, -1, 1001, 1.0, "1"])
def test_rpc_limit_cannot_bypass_query_integer_boundary(limit):
    with pytest.raises(HistoryValidationError):
        query().rpc_params(limit, None)


def test_full_transaction_keeps_exact_amounts_and_opaque_metadata_immutable():
    payload = transaction()
    payload["slot"] = MAX_U64
    payload["meta"].update(
        {
            "fee": MAX_U64,
            "preBalances": [MAX_U64],
            "postBalances": [MAX_U64 - 1],
            "preTokenBalances": [{"uiTokenAmount": {"amount": str(MAX_U64), "uiAmount": 0.5}}],
            "opaqueFutureMetadata": {"nested": ["kept", 0.25]},
        }
    )
    original = copy.deepcopy(payload)
    record = parse(payload)
    assert isinstance(record, HistoryTransaction)
    assert record.signature == SIGNATURE and record.transaction_index == 4
    assert record.raw_event_id == 7 and record.page_raw_event_id == 3
    assert record.slot == MAX_U64 and record.version is None
    assert record.transaction_error is None and record.status == "succeeded"
    assert json.loads(record.payload_json) == original == payload
    assert json.loads(record.payload_json)["meta"]["fee"] == MAX_U64
    payload["meta"]["opaqueFutureMetadata"]["nested"].append("later mutation")
    assert json.loads(record.payload_json) == original
    with pytest.raises(ValidationError):
        record.slot = 1


@pytest.mark.parametrize("version", [None, "legacy", 0, 1])
def test_supported_or_unreported_transaction_version_is_retained(version):
    payload = transaction()
    payload["version"] = version
    if version == 1:
        payload["transaction"]["message"]["transactionConfig"] = {
            "computeUnitLimit": 30000,
            "heapSize": None,
            "loadedAccountsDataSizeLimit": 200000,
            "priorityFee": None,
        }
    assert parse(payload).version == version
    assert json.loads(parse(payload).payload_json) == payload


@pytest.mark.parametrize("version", [True, False, 0.0, "0", 2, [], {}])
def test_unknown_or_coerced_versions_are_rejected(version):
    payload = transaction()
    payload["version"] = version
    with pytest.raises(HistoryValidationError) as raised:
        parse(payload)
    assert raised.value.code == "invalid_history_version"


@pytest.mark.parametrize(
    "error", [None, "BlockhashNotFound", {"InstructionError": [0, {"Custom": 1}]}]
)
def test_transaction_status_comes_only_from_explicit_typed_metadata_error(error):
    payload = transaction()
    payload["meta"]["err"] = error
    record = parse(payload)
    assert record.transaction_error == error
    assert record.status == ("succeeded" if error is None else "failed")


@pytest.mark.parametrize("error", [False, True, 0, 1.0, [], ["InstructionError"]])
def test_invalid_error_types_do_not_become_success_or_failure(error):
    payload = transaction()
    payload["meta"]["err"] = error
    with pytest.raises(HistoryValidationError) as raised:
        parse(payload)
    assert raised.value.code == "history_metadata_unavailable"


@pytest.mark.parametrize(
    "shape", ["missing", "null", "list", "missing_error", "missing_fee", "missing_balances"]
)
def test_unavailable_full_metadata_cannot_be_invented(shape):
    payload = transaction()
    if shape == "missing":
        del payload["meta"]
    elif shape == "null":
        payload["meta"] = None
    elif shape == "list":
        payload["meta"] = []
    elif shape == "missing_error":
        del payload["meta"]["err"]
    elif shape == "missing_fee":
        del payload["meta"]["fee"]
    else:
        del payload["meta"]["preBalances"]
    with pytest.raises(HistoryValidationError):
        parse(payload)


@pytest.mark.parametrize("field", ["fee", "preBalances", "postBalances"])
@pytest.mark.parametrize("amount", [True, -1, 1.0, "1", None, MAX_U64 + 1])
def test_raw_lamports_are_u64_integers_without_boolean_or_float_coercion(field, amount):
    payload = transaction()
    payload["meta"][field] = amount if field == "fee" else [amount]
    with pytest.raises(HistoryValidationError) as raised:
        parse(payload)
    assert raised.value.code == "invalid_history_amount"


@pytest.mark.parametrize(
    "amount", [1, 1.0, True, None, "-1", "1.0", "01", str(MAX_U64 + 1), "9" * 10000]
)
def test_token_raw_amount_is_canonical_u64_decimal_string(amount):
    payload = transaction()
    payload["meta"]["postTokenBalances"] = [{"uiTokenAmount": {"amount": amount}}]
    with pytest.raises(HistoryValidationError) as raised:
        parse(payload)
    assert raised.value.code == "invalid_history_amount"


def test_missing_token_balance_history_is_preserved_without_defaulting_to_empty():
    payload = transaction()
    payload["meta"]["preTokenBalances"] = None
    retained = json.loads(parse(payload).payload_json)["meta"]
    assert retained["preTokenBalances"] is None and "postTokenBalances" not in retained


def test_v0_loaded_account_resolution_matches_both_balance_arrays():
    payload = transaction()
    payload["version"] = 0
    payload["meta"].update(
        {
            "loadedAddresses": {"writable": [WALLET], "readonly": [WALLET]},
            "preBalances": [10000, 5, 3],
            "postBalances": [5000, 5, 3],
        }
    )
    assert parse(payload).version == 0
    payload["meta"]["postBalances"] = [5000]
    with pytest.raises(HistoryValidationError):
        parse(payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("slot", True),
        ("slot", -1),
        ("slot", MAX_U64 + 1),
        ("transactionIndex", None),
        ("transactionIndex", 1.0),
        ("transactionIndex", -1),
        ("blockTime", True),
        ("blockTime", 1.0),
        ("blockTime", 1 << 63),
    ],
)
def test_identity_and_timestamp_numbers_are_strict(field, value):
    payload = transaction()
    payload[field] = value
    with pytest.raises(HistoryValidationError):
        parse(payload)


def test_null_block_time_is_retained_as_unknown():
    payload = transaction()
    payload["blockTime"] = None
    assert parse(payload).block_time is None


@pytest.mark.parametrize(
    "change", ["message", "keys", "instructions", "signature", "unknown", "json"]
)
def test_malformed_or_unknown_full_transactions_have_only_fixed_safe_diagnostics(change):
    payload = transaction()
    secret = "synthetic-provider-error-detail"
    if change == "message":
        payload["transaction"]["message"] = {}
    elif change == "keys":
        payload["transaction"]["message"]["accountKeys"] = [{"pubkey": WALLET}]
    elif change == "instructions":
        payload["transaction"]["message"]["instructions"] = [secret]
    elif change == "signature":
        payload["transaction"]["signatures"] = [secret]
    elif change == "unknown":
        payload["providerSecret"] = secret
    else:
        payload["meta"]["future"] = float("nan")
    with pytest.raises(HistoryValidationError) as raised:
        parse(payload)
    assert secret not in str(raised.value)
    assert secret not in raised.value.code


@pytest.mark.parametrize("token", [None, "opaque-next-page"])
def test_page_parser_requires_explicit_completion_and_defers_transaction_validation(token):
    invalid_transaction = {"not": "parsed before persistence"}
    records, next_token = parse_history_page(
        {"result": {"data": [invalid_transaction], "paginationToken": token}}, limit=1
    )
    assert records == [invalid_transaction] and next_token == token
    assert parse_history_page({"result": {"data": [], "paginationToken": token}}, limit=1) == (
        [],
        token,
    )


@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        {"result": None},
        {"result": []},
        {"result": {"data": []}},
        {"result": {"data": [], "paginationToken": None, "extra": True}},
        {"result": {"data": {}, "paginationToken": None}},
        {"result": {"data": [1, 2], "paginationToken": None}},
        {"result": {"data": [], "paginationToken": False}},
    ],
)
def test_malformed_pages_never_imply_exhausted_history(response):
    with pytest.raises(HistoryValidationError):
        parse_history_page(response, limit=1)


@pytest.mark.parametrize("field", ["raw_event_id", "page_raw_event_id"])
@pytest.mark.parametrize("value", [0, -1, True, 1.0])
def test_history_records_require_real_positive_journal_ids(field, value):
    with pytest.raises(HistoryValidationError):
        parse_history_transaction(
            transaction(), **{"raw_event_id": 7, "page_raw_event_id": 3, field: value}
        )


@pytest.mark.parametrize("limit", [0, -1, True, 1.0, "1", 1001])
def test_page_caller_cannot_bypass_the_page_size_bound(limit):
    with pytest.raises(HistoryValidationError) as raised:
        parse_history_page({"result": {"data": [], "paginationToken": None}}, limit=limit)
    assert raised.value.code == "invalid_history_request"
