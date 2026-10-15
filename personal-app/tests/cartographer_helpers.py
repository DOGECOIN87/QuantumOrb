"""Synthetic canonical activity and journal-first history for offline graph tests."""

from __future__ import annotations

import asyncio
import copy
import struct
from datetime import UTC, datetime, timedelta

from app.config import BASE58_ALPHABET, Settings
from app.db.canonical_events import CanonicalEventRepository
from app.db.rpc_journal import RpcResponseJournal
from app.domain.keys import canonical_event_key
from app.domain.models import CanonicalSolanaEvent
from app.integrations.wallet_history import WalletHistoryAdapter
from app.integrations.wallet_history_models import HistoryQuery

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
SYSTEM = "11111111111111111111111111111111"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


def base58(data):
    number = int.from_bytes(data, "big")
    text = ""
    while number:
        number, digit = divmod(number, 58)
        text = BASE58_ALPHABET[digit] + text
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + text


def address(number):
    return base58(number.to_bytes(32, "big"))


def signature(number):
    return base58(number.to_bytes(64, "big"))


A, B, C, MINT = (address(number) for number in (1, 2, 3, 4))


def activity(
    factory,
    *,
    wallet=B,
    creator=None,
    mint=MINT,
    slot=300000020,
    index=1,
    instruction_index=1,
    age=60,
    number=90,
    finality="finalized",
    event_index=0,
    event_type="curve_buy",
):
    sig = signature(number)
    raw = RpcResponseJournal(factory, "helius_rpc", "test").insert(
        method="getTransaction",
        program_id=PUMP,
        params=[sig],
        response={"test_activity": number},
        received_at=NOW,
        signature=sig,
        slot=slot,
    )
    event_name = "CreateEvent" if event_type == "token_created" else "TradeEvent"
    key = canonical_event_key("helius", sig, PUMP, event_name, event_index)
    event = CanonicalSolanaEvent(
        event_key=key,
        signature=sig,
        slot=slot,
        block_time=NOW - timedelta(seconds=age),
        observed_at=NOW,
        commitment="finalized",
        program="pump",
        program_id=PUMP,
        event_name=event_name,
        event_type=event_type,
        event_index=event_index,
        mint=mint,
        user=wallet,
        creator=creator,
        transaction_index=index,
        instruction_index=instruction_index,
        payload={"test": True},
        decode_status="decoded",
        finality_status=finality,
        reconciliation_status="not_required",
        derivation_version="scout-0.1.0",
        raw_event_id=raw.id,
    )
    return CanonicalEventRepository(factory).insert_if_absent(event)


def transfer(
    *,
    sender=A,
    recipient=B,
    amount=700,
    slot=10,
    index=0,
    age=120,
    number=1,
    token=False,
    program=TOKEN,
    fee=None,
):
    keys = [sender, recipient, SYSTEM]
    instruction = {
        "programIdIndex": 2,
        "accounts": [0, 1],
        "data": base58(struct.pack("<IQ", 2, amount)),
    }
    before, after = [1_000_000, 0, 1], [995_000 - amount, amount, 1]
    pre_tokens, post_tokens = [], []
    if token:
        source, destination = address(20), address(21)
        keys = [sender, recipient, source, destination, MINT, program]
        raw = b"\x0c" + struct.pack("<QB", amount, 6)
        if fee is not None:
            raw = b"\x1a\x01" + struct.pack("<QBQ", amount, 6, fee)
        instruction = {"programIdIndex": 5, "accounts": [2, 4, 3, 0], "data": base58(raw)}
        before, after = [1_000_000] * 6, [995_000] + [1_000_000] * 5
        for account, owner, pre, post in (
            (2, sender, 100000, 100000 - amount),
            (3, recipient, 0, amount - (fee or 0)),
        ):

            def row(value, account=account, owner=owner):
                return {
                    "accountIndex": account,
                    "mint": MINT,
                    "owner": owner,
                    "programId": program,
                    "uiTokenAmount": {"amount": str(value), "decimals": 6},
                }

            pre_tokens.append(row(pre))
            post_tokens.append(row(post))
    return {
        "slot": slot + 300000000,
        "transactionIndex": index,
        "blockTime": int(NOW.timestamp()) - age,
        "version": "legacy",
        "transaction": {
            "signatures": [signature(number)],
            "message": {
                "accountKeys": keys,
                "header": {
                    "numRequiredSignatures": 1,
                    "numReadonlySignedAccounts": 0,
                    "numReadonlyUnsignedAccounts": 1,
                },
                "instructions": [instruction],
            },
        },
        "meta": {
            "err": None,
            "fee": 5000,
            "preBalances": before,
            "postBalances": after,
            "preTokenBalances": pre_tokens,
            "postTokenBalances": post_tokens,
            "innerInstructions": [],
            "logMessages": [],
        },
    }


def history(factory, *transactions, wallet=B, token_accounts="balanceChanged", token=None):
    class Rpc:
        async def rpc(self, method, params):
            return {"result": {"data": copy.deepcopy(list(transactions)), "paginationToken": token}}

        async def close(self):
            pass

    settings = Settings(
        _env_file=None,
        ingest_mode="live",
        ingest_transport="parsed_streams",
        helius_api_key="synthetic-cartographer-test-secret",
    )
    query = HistoryQuery(
        wallet=wallet,
        start_time=NOW - timedelta(minutes=10),
        end_time=NOW,
        page_size=max(len(transactions), 1),
        max_transactions=max(len(transactions), 1),
        token_accounts=token_accounts,
    )
    adapter = WalletHistoryAdapter(settings, factory, rpc=Rpc(), clock=lambda: NOW)
    return asyncio.run(adapter.fetch(query))
