"""Offline acceptance checks for bounded reconciliation coverage proofs."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import deque
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.config import BASE58_ALPHABET, Settings
from app.db.models import job_runs, raw_events
from app.db.raw_journal import canonical_payload_json
from app.db.session import create_all, create_database_engine, create_session_factory
from app.db.stream_cursors import StreamCursorRepository
from app.decoders import DecodeResult
from app.domain.enums import DecodeStatus
from app.domain.models import DecodedEvent, ProviderEnvelope
from app.integrations.raw_logs_decoder import RawLogsDecoder
from app.integrations.stream_recovery import BoundedStreamRecovery, RpcResponseJournal

NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)
SETTINGS = Settings(
    _env_file=None,
    app_env="test",
    ingest_mode="live",
    ingest_transport="logs_subscribe",
    helius_api_key="synthetic-recovery-test-secret",
)
PUMP = SETTINGS.pump_program_id
AMM = SETTINGS.pump_amm_program_id


def signature(number):
    data = number.to_bytes(64, "big")
    encoded = ""
    while number:
        number, digit = divmod(number, 58)
        encoded = BASE58_ALPHABET[digit] + encoded
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + encoded


ANCHOR = signature(1)
NEW = signature(2)


@pytest.fixture
def factory(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'recovery.db'}")
    create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def rows(factory, table=raw_events):
    with factory() as session:
        return list(session.execute(select(table)).mappings())


def checkpoint(factory, program=PUMP, *, slot=100, anchor=ANCHOR, age=10, settings=SETTINGS):
    repo = StreamCursorRepository(factory)
    if anchor is not None:
        payload = {"fixture": "durable-notification", "program": program, "signature": anchor}
        repo.journal(
            ProviderEnvelope(
                transport=settings.ingest_transport,
                notification_id=program + anchor,
                received_at=NOW - timedelta(seconds=age),
                commitment="confirmed",
                slot=slot,
                signature=anchor,
                raw_payload_sha256=hashlib.sha256(
                    canonical_payload_json(payload).encode()
                ).hexdigest(),
                idl_revision="fixture",
                raw_payload=payload,
            ),
            program,
        )
    return repo.begin_recovery(settings.ingest_transport, program, NOW - timedelta(seconds=age))


def entry(sig=NEW, slot=101, *, age=1, error=None, **updates):
    return {
        "signature": sig,
        "slot": slot,
        "err": error,
        "memo": None,
        "blockTime": int(NOW.timestamp()) - age,
        "confirmationStatus": "confirmed",
        **updates,
    }


def transaction(sig=NEW, slot=101, *, error=None):
    return {
        "slot": slot,
        "version": "legacy",
        "transaction": {
            "signatures": [sig],
            "message": {
                "accountKeys": [PUMP],
                "instructions": [{"programIdIndex": 0, "accounts": [], "data": ""}],
            },
        },
        "meta": {
            "err": error,
            "innerInstructions": [],
            "logMessages": [f"Program {PUMP} invoke [1]", f"Program {PUMP} success"],
        },
    }


class Rpc:
    def __init__(self, pages, transactions=None):
        self.pages = {program: deque(values) for program, values in pages.items()}
        self.slots = {
            item["signature"]: item["slot"]
            for values in pages.values()
            for page in values
            if isinstance(page, dict) and isinstance(page.get("result"), list)
            for item in page["result"]
            if isinstance(item, dict) and isinstance(item.get("signature"), str)
        }
        self.transactions = transactions or {}
        self.calls = []
        self.hook = None

    async def rpc(self, method, params):
        self.calls.append((method, params))
        if self.hook:
            self.hook(method, params)
        if method == "getSignaturesForAddress":
            value = self.pages[params[0]].popleft()
        else:
            value = self.transactions.get(
                params[0], {"result": transaction(params[0], self.slots.get(params[0], 101))}
            )
        if isinstance(value, BaseException):
            raise value
        return value


class Decoder:
    def __init__(self, factory, *, code=None):
        self.factory = factory
        self.code = code
        self.inputs = []
        self.hook = None

    def decode(self, value):
        self.inputs.append(value)
        # An independent connection sees the immutable bare body before decoding.
        retained = next(row for row in rows(self.factory) if row["id"] == value.raw_event_id)
        assert retained["payload_json"] == value.payload_json
        assert json.loads(retained["payload_json"])["transaction"]
        if self.hook:
            self.hook(value)
        if self.code:
            return DecodeResult(
                raw_event_id=value.raw_event_id,
                status=DecodeStatus.RAW_ONLY,
                safe_error_code=self.code,
                safe_error_message="synthetic safe decoder outcome",
            )
        return DecodeResult(
            raw_event_id=value.raw_event_id,
            status=DecodeStatus.DECODED,
            events=[
                DecodedEvent(
                    event_name="CreateEvent",
                    program_id=PUMP,
                    event_index=0,
                    fields={},
                    decode_status=DecodeStatus.DECODED,
                )
            ],
        )


def recover(factory, checkpoints, rpc, *, decoder=True, settings=SETTINGS, **options):
    consumer = []

    async def receive(result):
        consumer.append(result)

    selected = Decoder(factory) if decoder is True else decoder
    recovery = BoundedStreamRecovery(
        settings, factory, rpc=rpc, clock=lambda: NOW, decoder=selected, **options
    )
    report = asyncio.run(recovery.recover(checkpoints, on_result=receive))
    return report, consumer, selected


def test_anchor_overlap_recovers_same_slot_in_provider_order(factory):
    anchor = checkpoint(factory)
    same_slot = signature(3)
    rpc = Rpc(
        {
            PUMP: [
                {
                    "result": [
                        entry(),
                        entry(ANCHOR, 100),
                        entry(same_slot, 100),
                        entry(signature(4), 99),
                    ]
                }
            ]
        },
        {same_slot: {"result": transaction(same_slot, 100)}},
    )
    report, delivered, decoder = recover(factory, [anchor], rpc)
    assert not report.data_gap
    assert report.programs[0].hydrated == report.programs[0].delivered == len(delivered) == 3
    assert [json.loads(item.payload_json)["slot"] for item in decoder.inputs] == [101, 100, 100]
    stored = StreamCursorRepository(factory).load("logs_subscribe", PUMP)
    assert stored.pending_since is None and not stored.data_gap
    assert stored.slot == 100 and stored.signature == ANCHOR
    assert rows(factory, job_runs)[0]["status"] == "success"


def test_page_through_same_slot_without_until_or_lexical_comparison(factory):
    anchor = checkpoint(factory, anchor=signature(4))
    rpc = Rpc(
        {
            PUMP: [
                {"result": [entry(signature(4), 100), entry(signature(3), 100)]},
                {"result": [entry(signature(5), 100), entry(signature(6), 99)]},
            ]
        },
        {
            signature(3): {"result": transaction(signature(3), 100)},
            signature(5): {"result": transaction(signature(5), 100)},
        },
    )
    report, delivered, _ = recover(factory, [anchor], rpc, page_size=2)
    assert not report.data_gap and len(delivered) == 3
    listing = [params[1] for method, params in rpc.calls if method == "getSignaturesForAddress"]
    assert listing == [
        {"commitment": "confirmed", "limit": 2},
        {"commitment": "confirmed", "limit": 2, "before": signature(3)},
    ]


def test_global_budget_includes_both_programs_and_marks_uncovered_program(factory):
    anchors = [checkpoint(factory, program) for program in (PUMP, AMM)]
    rpc = Rpc({PUMP: [{"result": [entry(), entry(ANCHOR, 100)]}]})
    report, delivered, _ = recover(factory, anchors, rpc, max_signatures=2)
    assert report.data_gap
    assert report.programs[1].gap_reason == "recovery_signature_limit"
    assert sum(program.listed for program in report.programs) == 2
    assert len(delivered) == 2
    assert len([call for call in rpc.calls if call[0] == "getSignaturesForAddress"]) == 1


def test_duplicate_across_programs_is_hydrated_and_delivered_once(factory):
    anchors = [checkpoint(factory, program) for program in (PUMP, AMM)]
    page = {"result": [entry(), entry(ANCHOR, 100)]}
    rpc = Rpc({PUMP: [page], AMM: [page]})
    report, delivered, decoder = recover(factory, anchors, rpc)
    assert not report.data_gap and len(delivered) == len(decoder.inputs) == 2
    assert [item.hydrated for item in report.programs] == [2, 0]


def test_replay_does_not_duplicate_raw_payload_but_redelivers_canonical_work(factory):
    anchor = checkpoint(factory)
    page = {"result": [entry(), entry(ANCHOR, 100)]}
    first, delivered, _ = recover(factory, [anchor], Rpc({PUMP: [page]}))
    count = len(rows(factory))
    restarted = StreamCursorRepository(factory).begin_recovery("logs_subscribe", PUMP, NOW)
    second, again, _ = recover(factory, [restarted], Rpc({PUMP: [page]}))
    assert len(rows(factory)) == count
    assert len(delivered) == len(again) == 2
    assert first.run_id != second.run_id and len(rows(factory, job_runs)) == 2


@pytest.mark.parametrize(
    "page,reason",
    [
        ({"result": None}, "invalid_signature_response"),
        ({"result": {}}, "invalid_signature_response"),
        ({"result": [None]}, "invalid_signature_response"),
        ({"error": {"message": "synthetic"}}, "invalid_signature_response"),
        ({"result": [entry(signature=42)]}, "invalid_signature_response"),
        ({"result": [entry(slot=True)]}, "invalid_signature_response"),
        ({"result": [entry(confirmationStatus={})]}, "invalid_signature_response"),
        ({"result": [entry(blockTime=True)]}, "invalid_signature_response"),
        ({"result": [entry(extra=True)]}, "invalid_signature_response"),
        ({"result": [entry(error=False)]}, "invalid_signature_response"),
        ({"result": [entry(error=0)]}, "invalid_signature_response"),
        ({"result": [entry(error=[])]}, "invalid_signature_response"),
        ({"result": [entry(memo=[])]}, "invalid_signature_response"),
        ({"result": []}, "recovery_anchor_missing"),
    ],
)
def test_every_invalid_or_empty_response_is_journaled_before_interpretation(factory, page, reason):
    anchor = checkpoint(factory)
    report, delivered, _ = recover(factory, [anchor], Rpc({PUMP: [page]}))
    assert report.programs[0].gap_reason == reason and not delivered
    retained = json.loads(rows(factory)[-1]["payload_json"])
    assert retained["response"] == page
    assert rows(factory)[-1]["signature"] is None


@pytest.mark.parametrize(
    "result,code",
    [
        (None, "recovery_transaction_unavailable"),
        ([], "recovery_transaction_unavailable"),
        ({}, "recovery_transaction_identity_mismatch"),
        (transaction(ANCHOR), "recovery_transaction_identity_mismatch"),
        ({**transaction(), "slot": True}, "recovery_transaction_identity_mismatch"),
        ({**transaction(), "meta": None}, "recovery_transaction_metadata_unavailable"),
        ({**transaction(), "meta": {"err": False}}, "recovery_transaction_metadata_unavailable"),
    ],
)
def test_unavailable_or_unverifiable_transaction_is_retained_and_is_gap(factory, result, code):
    anchor = checkpoint(factory)
    rpc = Rpc({PUMP: [{"result": [entry(), entry(ANCHOR, 100)]}]}, {NEW: {"result": result}})
    report, delivered, decoder = recover(factory, [anchor], rpc)
    assert report.data_gap and len(delivered) == len(decoder.inputs) == 1
    assert json.loads(decoder.inputs[0].payload_json)["transaction"]["signatures"] == [ANCHOR]
    assert any(row["safe_error_code"] == code for row in rows(factory))
    assert rows(factory, job_runs)[0]["status"] == "partial_success"


@pytest.mark.parametrize(
    "code,expected_gap",
    [
        ("no_supported_event", False),
        ("failed_transaction", False),
        ("unknown_event", True),
        ("incomplete_logs", True),
    ],
)
def test_decoder_outcomes_distinguish_inspected_non_event_from_gap(factory, code, expected_gap):
    anchor = checkpoint(factory)
    rpc = Rpc({PUMP: [{"result": [entry(), entry(ANCHOR, 100)]}]})
    report, delivered, _ = recover(factory, [anchor], rpc, decoder=Decoder(factory, code=code))
    assert report.data_gap is expected_gap and not delivered
    assert report.programs[0].excluded == (0 if expected_gap else 2)


def test_failed_listing_transaction_never_fetched(factory):
    anchor = checkpoint(factory)
    rpc = Rpc(
        {PUMP: [{"result": [entry(error={"InstructionError": [0, "Custom"]}), entry(ANCHOR, 100)]}]}
    )
    report, delivered, decoder = recover(factory, [anchor], rpc)
    assert not report.data_gap and len(delivered) == len(decoder.inputs) == 1
    assert report.programs[0].excluded == 1
    assert [params[0] for method, params in rpc.calls if method == "getTransaction"] == [ANCHOR]


def test_failed_fetched_transaction_is_excluded_before_decoder(factory):
    anchor = checkpoint(factory)
    rpc = Rpc(
        {PUMP: [{"result": [entry(), entry(ANCHOR, 100)]}]},
        {
            NEW: {"result": transaction(error={"InstructionError": [0, "Custom"]})},
        },
    )
    report, delivered, decoder = recover(factory, [anchor], rpc)
    assert not report.data_gap and len(delivered) == len(decoder.inputs) == 1
    assert json.loads(decoder.inputs[0].payload_json)["transaction"]["signatures"] == [ANCHOR]
    assert report.programs[0].excluded == 1


@pytest.mark.parametrize("age,expected", [(119, False), (121, True)])
def test_time_window_is_fixed_and_bounded(factory, age, expected):
    anchor = checkpoint(factory)
    rpc = Rpc({PUMP: [{"result": [entry(age=age), entry(ANCHOR, 100)]}]})
    report, delivered, _ = recover(factory, [anchor], rpc)
    assert report.data_gap is expected and len(delivered) == (0 if expected else 2)


def test_old_anchor_is_gap_without_hydrating_outside_hard_time_window(factory):
    anchor = checkpoint(factory, age=1000)
    rpc = Rpc(
        {PUMP: [{"result": [entry(ANCHOR, 100, age=1000), entry(signature(5), 99, age=1100)]}]}
    )
    report, delivered, _ = recover(factory, [anchor], rpc)
    assert report.programs[0].gap_reason == "recovery_anchor_outside_window" and not delivered
    assert all(method == "getSignaturesForAddress" for method, _ in rpc.calls)


def test_unknown_block_time_leaves_gap_even_after_anchor_found(factory):
    anchor = checkpoint(factory)
    rpc = Rpc({PUMP: [{"result": [entry(blockTime=None), entry(ANCHOR, 100)]}]})
    report, delivered, _ = recover(factory, [anchor], rpc)
    assert (
        report.programs[0].gap_reason == "recovery_block_time_unavailable" and len(delivered) == 1
    )


@pytest.mark.parametrize(
    "entries",
    [
        [entry(), entry()],
        [entry(slot=101), entry(signature(3), 102)],
    ],
)
def test_duplicate_or_reverse_slot_pagination_is_explicit_gap(factory, entries):
    anchor = checkpoint(factory)
    report, delivered, _ = recover(factory, [anchor], Rpc({PUMP: [{"result": entries}]}))
    assert report.programs[0].gap_reason == "recovery_invalid_pagination"
    assert report.programs[0].hydrated == len(delivered) == 1


@pytest.mark.parametrize("age,gap", [(10, False), (121, True)])
def test_bootstrap_only_covers_recent_pending_boundary(factory, age, gap):
    anchor = checkpoint(factory, anchor=None, age=age)
    report, delivered, _ = recover(factory, [anchor], Rpc({PUMP: [{"result": []}]}))
    assert report.data_gap is gap and not delivered


def test_pending_bootstrap_stops_at_time_boundary_without_fetching_older_history(factory):
    anchor = checkpoint(factory, anchor=None, age=10)
    rpc = Rpc({PUMP: [{"result": [entry(age=1), entry(signature(3), 100, age=11)]}]})
    report, delivered, _ = recover(factory, [anchor], rpc)
    assert not report.data_gap and len(delivered) == 1


def test_bootstrap_without_a_recovery_start_boundary_is_honest_gap(factory):
    anchor = replace(checkpoint(factory, anchor=None), pending_since=None)
    report, _, _ = recover(factory, [anchor], Rpc({PUMP: [{"result": []}]}))
    assert report.programs[0].gap_reason == "recovery_no_baseline"


def test_listing_is_committed_before_next_http_fetch(factory):
    anchor = checkpoint(factory)
    page = {"result": [entry(), entry(ANCHOR, 100)]}
    rpc = Rpc({PUMP: [page]})

    def inspect(method, params):
        if method == "getTransaction":
            assert any(
                json.loads(row["payload_json"]).get("response") == page for row in rows(factory)
            )
            assert params == [
                params[0],
                {
                    "commitment": "confirmed",
                    "encoding": "json",
                    "maxSupportedTransactionVersion": 1,
                },
            ]
            assert params[0] in {NEW, ANCHOR}

    rpc.hook = inspect
    report, _, _ = recover(factory, [anchor], rpc)
    assert not report.data_gap


def test_default_decoder_accepts_explicit_parsed_stream_rpc_recovery(factory):
    settings = SETTINGS.model_copy(update={"ingest_transport": "parsed_streams"})
    anchor = checkpoint(factory, settings=settings)
    rpc = Rpc({PUMP: [{"result": [entry(), entry(ANCHOR, 100)]}]})
    report, delivered, _ = recover(factory, [anchor], rpc, settings=settings, decoder=None)
    assert not report.data_gap and not delivered
    body = next(row for row in rows(factory) if row["safe_error_code"] == "no_supported_event")
    assert body["transport"] == "parsed_streams"
    assert RawLogsDecoder(settings)._accepted_transport == "logs_subscribe"


def test_consumer_failure_is_audited_and_propagated_with_anchor_preserved(factory):
    anchor = checkpoint(factory)
    rpc = Rpc({PUMP: [{"result": [entry(), entry(ANCHOR, 100)]}]})
    recovery = BoundedStreamRecovery(
        SETTINGS, factory, rpc=rpc, clock=lambda: NOW, decoder=Decoder(factory)
    )

    async def fail(result):
        raise RuntimeError("synthetic consumer failed")

    with pytest.raises(RuntimeError, match="synthetic consumer failed"):
        asyncio.run(recovery.recover([anchor], on_result=fail))
    audit = rows(factory, job_runs)[0]
    assert audit["status"] == "failed" and audit["safe_error_code"] == "recovery_consumer_failed"
    counts = json.loads(audit["counts_json"])["programs"][0]
    assert not counts["covered"] and counts["hydrated"] == 1 and counts["delivered"] == 0
    stored = StreamCursorRepository(factory).load("logs_subscribe", PUMP)
    assert stored.pending_since is not None and stored.anchor_signature == ANCHOR


def test_cancellation_finishes_audit_and_preserves_recovery_anchor(factory):
    anchor = checkpoint(factory)
    rpc = Rpc({PUMP: [{"result": [entry(), entry(ANCHOR, 100)]}]})
    recovery = BoundedStreamRecovery(
        SETTINGS, factory, rpc=rpc, clock=lambda: NOW, decoder=Decoder(factory)
    )

    async def cancel(result):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(recovery.recover([anchor], on_result=cancel))
    assert rows(factory, job_runs)[0]["safe_error_code"] == "recovery_interrupted"
    assert StreamCursorRepository(factory).load("logs_subscribe", PUMP).pending_since is not None


def test_fetch_timeout_is_explicit_gap(factory):
    anchor = checkpoint(factory)
    rpc = Rpc({PUMP: [{"result": [entry(), entry(ANCHOR, 100)]}]})
    original = rpc.rpc

    async def slow(method, params):
        if method == "getTransaction":
            await asyncio.sleep(60)
        return await original(method, params)

    rpc.rpc = slow
    report, delivered, _ = recover(factory, [anchor], rpc, rpc_timeout=0.01)
    assert report.programs[0].gap_reason == "recovery_rpc_failed" and not delivered
    assert report.programs[0].hydrated == 1


@pytest.mark.parametrize(
    "option,value",
    [
        ("max_signatures", 0),
        ("max_signatures", 501),
        ("max_signatures", True),
        ("window_seconds", 0),
        ("window_seconds", 121),
        ("window_seconds", 1.5),
        ("page_size", 0),
        ("page_size", 1001),
        ("page_size", False),
        ("rpc_timeout", float("nan")),
        ("rpc_timeout", float("inf")),
        ("rpc_timeout", True),
        ("rpc_timeout", 0),
        ("rpc_timeout", "1"),
    ],
)
def test_input_bounds_fail_before_rpc(factory, option, value):
    with pytest.raises(ValueError):
        BoundedStreamRecovery(SETTINGS, factory, rpc=Rpc({}), **{option: value})


def test_journal_identity_includes_request_context_and_transport(factory):
    first = RpcResponseJournal(factory, "logs_subscribe", "fixture")
    second = RpcResponseJournal(factory, "parsed_streams", "fixture")
    options = dict(
        method="getSignaturesForAddress", program_id=PUMP, params=[], response=None, received_at=NOW
    )
    row = first.insert(**options)
    assert first.insert(**options).id == row.id
    assert first.insert(**{**options, "program_id": AMM}).id != row.id
    assert second.insert(**options).id != row.id
    assert row.signature is None and row.slot is None


def test_cursor_anchor_is_replayed_after_crash_before_decode_or_consumer(factory):
    anchor = checkpoint(factory)
    # Only the raw notification exists; no previous decode/consumer commit is known.
    assert rows(factory)[0]["status"] == "new"
    rpc = Rpc({PUMP: [{"result": [entry(ANCHOR, 100)]}]})
    report, delivered, decoder = recover(factory, [anchor], rpc)
    assert not report.data_gap and len(delivered) == len(decoder.inputs) == 1
    assert json.loads(decoder.inputs[0].payload_json)["transaction"]["signatures"] == [ANCHOR]


def test_unavailable_anchor_never_claims_coverage(factory):
    anchor = checkpoint(factory)
    rpc = Rpc({PUMP: [{"result": [entry(ANCHOR, 100)]}]}, {ANCHOR: {"result": None}})
    report, delivered, _ = recover(factory, [anchor], rpc)
    assert report.data_gap and not delivered


def test_null_time_then_old_entry_stops_at_time_boundary_without_extra_page(factory):
    anchor = checkpoint(factory)
    rpc = Rpc({PUMP: [{"result": [entry(blockTime=None), entry(signature(3), 100, age=121)]}]})
    report, delivered, _ = recover(factory, [anchor], rpc, page_size=2)
    assert report.programs[0].gap_reason == "recovery_block_time_unavailable" and not delivered
    assert len(rpc.calls) == 1


def test_quiet_restart_uses_original_watch_time_as_baseline(factory):
    anchor = checkpoint(factory, anchor=None, age=60)
    anchor = replace(anchor, pending_since=NOW)
    rpc = Rpc({PUMP: [{"result": [entry(age=10), entry(signature(3), 100, age=61)]}]})
    report, delivered, _ = recover(factory, [anchor], rpc)
    assert not report.data_gap and len(delivered) == 1


def test_subsecond_watch_boundary_does_not_skip_same_second_transaction(factory):
    anchor = checkpoint(factory, anchor=None, age=10)
    anchor = replace(anchor, anchor_received_at=NOW - timedelta(seconds=9.5))
    rpc = Rpc({PUMP: [{"result": [entry(age=10), entry(signature(3), 100, age=11)]}]})
    report, delivered, _ = recover(factory, [anchor], rpc)
    assert not report.data_gap and len(delivered) == 1


def test_default_five_hundred_listing_bound_caps_request_even_with_larger_page(factory):
    anchor = checkpoint(factory)
    candidates = [
        entry(signature(index + 2), 1001 - index, error="AccountNotFound") for index in range(500)
    ]
    rpc = Rpc({PUMP: [{"result": candidates}]})
    report, delivered, _ = recover(factory, [anchor], rpc, page_size=1000)
    assert report.programs[0].gap_reason == "recovery_signature_limit" and not delivered
    assert report.programs[0].listed == report.programs[0].excluded == 500
    assert rpc.calls == [
        ("getSignaturesForAddress", [PUMP, {"commitment": "confirmed", "limit": 500}])
    ]
