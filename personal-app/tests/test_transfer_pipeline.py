"""Offline journal-to-transfer integration and replay provenance checks."""

from __future__ import annotations

import asyncio
import copy
import json
import struct
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.config import BASE58_ALPHABET, Settings
from app.db.models import metadata, raw_events
from app.db.session import create_all, create_database_engine, create_session_factory
from app.domain.enums import EvidenceState
from app.integrations import wallet_history as history_module
from app.integrations.wallet_history import WalletHistoryAdapter
from app.integrations.wallet_history_models import HistoryQuery
from app.transfers import TransferExtractor

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
SYSTEM = "11111111111111111111111111111111"


def base58(data):
    number = int.from_bytes(data, "big")
    encoded = ""
    while number:
        number, digit = divmod(number, 58)
        encoded = BASE58_ALPHABET[digit] + encoded
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + encoded


PAYER = base58((1).to_bytes(32, "big"))
RECIPIENT = base58((2).to_bytes(32, "big"))
SIGNATURE = base58((1).to_bytes(64, "big"))


def transaction():
    return {
        "slot": 300000007,
        "transactionIndex": 4,
        "blockTime": int(NOW.timestamp()) - 60,
        "version": "legacy",
        "transaction": {
            "signatures": [SIGNATURE],
            "message": {
                "header": {
                    "numRequiredSignatures": 1,
                    "numReadonlySignedAccounts": 0,
                    "numReadonlyUnsignedAccounts": 1,
                },
                "accountKeys": [PAYER, RECIPIENT, SYSTEM],
                "instructions": [
                    {
                        "programIdIndex": 2,
                        "accounts": [0, 1],
                        "data": base58(struct.pack("<IQ", 2, 700)),
                    }
                ],
            },
        },
        "meta": {
            "err": None,
            "fee": 5000,
            "preBalances": [10000, 1000, 1],
            "postBalances": [4300, 1700, 1],
            "preTokenBalances": [],
            "postTokenBalances": [],
            "innerInstructions": [],
            "logMessages": [],
        },
    }


class Rpc:
    def __init__(self, data):
        self.data = data
        self.calls = []

    async def rpc(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        return {"result": {"data": [copy.deepcopy(self.data)], "paginationToken": None}}

    async def close(self):
        raise AssertionError("An injected reader must remain owned by the caller")


@pytest.fixture
def factory(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'transfer-pipeline.db'}")
    create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def snapshot(factory):
    with factory() as session:
        return {
            table.name: tuple(session.execute(select(table)).all())
            for table in metadata.sorted_tables
        }


def fetch(factory, data, wallet=PAYER):
    rpc = Rpc(data)
    settings = Settings(
        _env_file=None,
        app_env="test",
        ingest_mode="live",
        ingest_transport="parsed_streams",
        helius_api_key="synthetic-transfer-pipeline-secret",
    )
    query = HistoryQuery(
        wallet=wallet,
        start_time=NOW - timedelta(minutes=10),
        end_time=NOW,
        token_accounts="none",
        status="any",
        page_size=1,
        max_transactions=1,
    )
    adapter = WalletHistoryAdapter(settings, factory, rpc=rpc, clock=lambda: NOW)
    return asyncio.run(adapter.fetch(query)), rpc


def test_journal_precedes_parsing_and_replays_preserve_fact_identity(factory, monkeypatch):
    original = history_module.parse_history_transaction
    parsed_refs = []

    def assert_journaled(payload, *, raw_event_id, page_raw_event_id):
        with factory() as session:
            rows = {row.id: row for row in session.execute(select(raw_events)).mappings().all()}
        assert raw_event_id != page_raw_event_id
        assert json.loads(rows[raw_event_id].payload_json) == payload
        assert rows[raw_event_id].signature == SIGNATURE
        assert rows[raw_event_id].commitment == "finalized"
        retained_page = json.loads(rows[page_raw_event_id].payload_json)
        assert retained_page["response"]["result"]["data"] == [payload]
        parsed_refs.append((raw_event_id, page_raw_event_id))
        return original(payload, raw_event_id=raw_event_id, page_raw_event_id=page_raw_event_id)

    monkeypatch.setattr(history_module, "parse_history_transaction", assert_journaled)
    results, facts = [], []
    for wallet in (PAYER, PAYER, RECIPIENT):
        result, rpc = fetch(factory, transaction(), wallet)
        results.append(result)
        assert result.coverage.history_complete and len(result.transactions) == 1
        assert rpc.calls[0][0] == "getTransactionsForAddress"
        before = snapshot(factory)
        extraction = TransferExtractor().extract(result.transactions[0])
        assert snapshot(factory) == before
        assert extraction.complete and extraction.state is EvidenceState.OBSERVED
        assert len(extraction.transfers) == 1
        fact = extraction.transfers[0]
        assert (fact.source_account, fact.destination_account, fact.amount_raw) == (
            PAYER,
            RECIPIENT,
            "700",
        )
        assert (fact.signature, fact.slot, fact.transaction_index) == (SIGNATURE, 300000007, 4)
        assert (fact.raw_event_id, fact.page_raw_event_id) == parsed_refs[-1]
        facts.append(fact)

    assert len({result.run_id for result in results}) == 3
    assert parsed_refs[0] == parsed_refs[1]
    assert parsed_refs[2][0] != parsed_refs[0][0]
    assert parsed_refs[2][1] != parsed_refs[0][1]
    assert len({fact.fact_key for fact in facts}) == 1
    with factory() as session:
        assert len(session.execute(select(raw_events)).all()) == 4


@pytest.mark.parametrize("case", ["failed", "invalid_compiled_index"])
def test_history_preserves_raw_evidence_when_extraction_has_no_successful_fact(factory, case):
    data = transaction()
    if case == "failed":
        data["meta"]["err"] = {"InstructionError": [0, "InsufficientFunds"]}
        data["meta"]["postBalances"] = [5000, 1000, 1]
    else:
        data["transaction"]["message"]["instructions"][0]["accounts"] = [0, 255]
    history, _ = fetch(factory, data)
    assert history.coverage.history_complete and len(history.transactions) == 1
    before = snapshot(factory)
    extraction = TransferExtractor().extract(history.transactions[0])
    assert snapshot(factory) == before
    assert not extraction.transfers
    assert extraction.raw_event_id == history.transactions[0].raw_event_id
    assert extraction.page_raw_event_id == history.page_raw_event_ids[0]
    if case == "failed":
        assert extraction.state is EvidenceState.NOT_APPLICABLE
        assert extraction.complete and "failed_transaction" in extraction.warnings
    else:
        assert extraction.state is EvidenceState.ERROR
        assert not extraction.complete and extraction.warnings == ("transfer_input_invalid",)
