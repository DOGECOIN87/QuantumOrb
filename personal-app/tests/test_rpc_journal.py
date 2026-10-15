"""Shared RPC evidence journal, identity compatibility, and safe rejection tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.base import Base
from app.db.models import raw_events
from app.db.rpc_journal import RpcJournalError, RpcResponseJournal
from app.domain.enums import Transport
from app.integrations.stream_recovery import RpcResponseJournal as RecoveryRpcResponseJournal

NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)
ADDRESS = "6EF8rrecthR5DkzPWsYzX9Z9u18RYPWv9VKn6kXHEfU"
SIGNATURE = "1" * 64
REVISION = "t021:helius-history-v1"


@pytest.fixture
def factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine(f"sqlite:///{tmp_path / 'rpc-journal.db'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


def options(**changes: Any) -> dict[str, Any]:
    return {
        "method": "getSignaturesForAddress",
        "program_id": ADDRESS,
        "params": [ADDRESS, {"limit": 2, "commitment": "confirmed"}],
        "response": {"result": None},
        "received_at": NOW,
        **changes,
    }


def wallet_journal(factory: sessionmaker[Session], **changes: Any) -> RpcResponseJournal:
    return RpcResponseJournal(
        factory,
        "helius_rpc",
        REVISION,
        **{"notification_prefix": "wallet-history-rpc", **changes},
    )


def test_default_recovery_identity_is_byte_compatible(factory: sessionmaker[Session]) -> None:
    assert RecoveryRpcResponseJournal is RpcResponseJournal
    row = RpcResponseJournal(factory, "logs_subscribe", "fixture").insert(**options())
    assert row.payload_sha256 == "86f46922a65029d0ccc856f186003703548fbb740e21e2169be72f1c7f128743"
    assert row.notification_id == (
        "recovery-rpc:edd8492f0150a48ef0c6dbdae66b176c5ac61eae410c9cbf77f44be3d4b02f5e"
    )
    assert row.event_key == "eaea31b367a8395bea51f1949d4ec7effe0a639d8e60a968595797a3dd21c532"
    assert row.commitment == "confirmed"


@pytest.mark.parametrize("response", [None, {"result": None}, {"result": []}, {}, [1, "2"], False])
def test_generic_rpc_retains_null_empty_and_unrecognized_shapes_without_signature(
    factory: sessionmaker[Session], response: Any
) -> None:
    row = wallet_journal(factory).insert(**options(response=response))
    assert json.loads(row.payload_json)["response"] == response
    assert row.signature is None and row.slot is None
    assert row.transport == Transport.HELIUS_RPC
    assert row.idl_revision == REVISION
    assert row.notification_id.startswith("wallet-history-rpc:")
    assert row.received_at == NOW and row.created_at == NOW
    assert row.status == "new"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(raw_events)) == 1


def test_identity_includes_transport_prefix_request_context_and_payload(
    factory: sessionmaker[Session],
) -> None:
    wallet = wallet_journal(factory)
    first = wallet.insert(**options())
    rows = [
        RpcResponseJournal(factory, "logs_subscribe", REVISION).insert(**options()),
        RpcResponseJournal(factory, "helius_rpc", REVISION).insert(**options()),
        wallet.insert(**options(method="getTransactionsForAddress")),
        wallet.insert(**options(program_id="11111111111111111111111111111111")),
        wallet.insert(**options(params=[ADDRESS, {"limit": 1}])),
        wallet.insert(**options(response={"result": []})),
    ]
    assert len({row.id for row in [first, *rows]}) == 7
    assert rows[0].payload_sha256 == first.payload_sha256 == rows[1].payload_sha256


def test_bare_transaction_retains_exact_payload_and_request_context_identity(
    factory: sessionmaker[Session],
) -> None:
    journal = wallet_journal(factory)
    transaction = {"slot": 12, "transaction": {"signatures": [SIGNATURE]}, "meta": None}
    first = journal.insert(**options(response=transaction, signature=SIGNATURE, slot=12, bare=True))
    assert json.loads(first.payload_json) == transaction
    assert first.signature == SIGNATURE and first.slot == 12
    assert "request" not in json.loads(first.payload_json)
    changed_context = journal.insert(
        **options(
            params=[ADDRESS, {"limit": 1}],
            response=transaction,
            signature=SIGNATURE,
            slot=12,
            bare=True,
        )
    )
    assert first.payload_sha256 == changed_context.payload_sha256
    assert first.event_key != changed_context.event_key


def test_bare_null_does_not_invent_a_transaction(factory: sessionmaker[Session]) -> None:
    row = wallet_journal(factory).insert(**options(response=None, bare=True))
    assert row.payload_json == "null"
    assert row.signature is None and row.slot is None


def test_duplicate_preserves_first_timestamp_revision_and_decode_status(
    factory: sessionmaker[Session],
) -> None:
    first = wallet_journal(factory).insert(**options())
    with factory() as session:
        session.execute(update(raw_events).values(status="raw_only"))
        session.commit()
    second = RpcResponseJournal(
        factory, "helius_rpc", "later-revision", notification_prefix="wallet-history-rpc"
    ).insert(**options(received_at=NOW + timedelta(minutes=1)))
    assert first.id == second.id
    assert second.received_at == first.received_at
    assert second.created_at == first.created_at
    assert second.idl_revision == first.idl_revision
    assert second.status == "raw_only"


def test_configured_commitment_is_retained_and_context_distinguishes_it(
    factory: sessionmaker[Session],
) -> None:
    confirmed = wallet_journal(factory).insert(**options())
    finalized = wallet_journal(factory, commitment="finalized").insert(
        **options(params=[ADDRESS, {"limit": 2, "commitment": "finalized"}])
    )
    assert finalized.commitment == "finalized"
    assert finalized.event_key != confirmed.event_key
    with pytest.raises(RpcJournalError) as raised:
        wallet_journal(factory, commitment="finalized").insert(**options())
    assert raised.value.code == "rpc_journal_conflict"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(raw_events)) == 2


def test_different_supplied_transaction_metadata_cannot_relabel_existing_evidence(
    factory: sessionmaker[Session],
) -> None:
    journal = wallet_journal(factory)
    journal.insert(**options(signature=SIGNATURE, slot=12))
    with pytest.raises(RpcJournalError) as raised:
        journal.insert(**options(signature=SIGNATURE, slot=13))
    assert raised.value.code == "rpc_journal_conflict"
    with factory() as session:
        assert session.scalar(select(raw_events.c.slot)) == 12


def test_aware_timestamps_normalize_to_utc_and_naive_input_is_rejected(
    factory: sessionmaker[Session],
) -> None:
    offset = timezone(timedelta(hours=-5))
    row = wallet_journal(factory).insert(**options(received_at=NOW.astimezone(offset)))
    assert row.received_at == NOW and row.received_at.tzinfo == UTC
    with pytest.raises(RpcJournalError) as raised:
        wallet_journal(factory).insert(**options(received_at=NOW.replace(tzinfo=None)))
    assert raised.value.code == "rpc_journal_invalid_context"


@pytest.mark.parametrize("response", [float("nan"), float("inf"), {"value": {1, 2}}, object()])
def test_unjournalable_payload_returns_only_fixed_diagnostic(
    factory: sessionmaker[Session], response: Any
) -> None:
    with pytest.raises(RpcJournalError) as raised:
        wallet_journal(factory).insert(**options(response=response))
    assert raised.value.code == "rpc_response_unjournalable"
    assert str(raised.value) == "RPC response journaling failed"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(raw_events)) == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"transport": "unknown"},
        {"commitment": "unknown"},
        {"notification_prefix": "secret=invalid"},
        {"notification_prefix": ""},
        {"revision": ""},
    ],
)
def test_invalid_journal_configuration_is_rejected(
    factory: sessionmaker[Session], changes: dict[str, Any]
) -> None:
    with pytest.raises(RpcJournalError) as raised:
        RpcResponseJournal(factory, **{"transport": "helius_rpc", "revision": REVISION, **changes})
    assert raised.value.code == "rpc_journal_invalid_config"
    assert "secret" not in str(raised.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"slot": True},
        {"slot": -1},
        {"signature": "secret=invalid"},
        {"params": {}},
        {"program_id": ""},
        {"method": ""},
        {"bare": "true"},
    ],
)
def test_invalid_request_metadata_does_not_mutate_journal(
    factory: sessionmaker[Session], changes: dict[str, Any]
) -> None:
    with pytest.raises(RpcJournalError) as raised:
        wallet_journal(factory).insert(**options(**changes))
    assert raised.value.code == "rpc_journal_invalid_context"
    assert "secret" not in str(raised.value)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(raw_events)) == 0


def test_new_raw_transport_cannot_be_selected_for_live_ingestion() -> None:
    assert Transport.HELIUS_RPC.value == "helius_rpc"
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            ingest_mode="live",
            ingest_transport="helius_rpc",
            helius_api_key="synthetic-test-key",
        )


def test_shared_session_remains_usable(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        journal = RpcResponseJournal(session, "helius_rpc", REVISION)
        first = journal.insert(**options())
        second = journal.insert(**options())
        assert first.id == second.id
        assert session.scalar(select(func.count()).select_from(raw_events)) == 1
