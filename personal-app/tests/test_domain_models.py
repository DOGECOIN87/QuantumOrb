import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.domain.enums import EvidenceState, GraphEdgeStrength, Transport
from app.domain.models import (
    CanonicalSolanaEvent,
    CartographerEvidenceUpdated,
    DecodedInstruction,
    EvidenceResult,
    GraphEdge,
    GraphEdgeEvidence,
    GraphNode,
    ProviderEnvelope,
    ScoutEventRecord,
)

ROOT = Path(__file__).parents[2]
SIGNATURE = (
    "5xSKzM8bvpudE521jikHqASzMr23Ms4X4ieY3K8oFPFrJWCSSgYocJmHznrR8b12voDxKDH8ykdCLXSRrx6duVLH"
)
EVENT_KEY = "a" * 64
NOW = datetime(2026, 10, 13, tzinfo=UTC)


def payload_hash(payload: dict[str, object]) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def test_provider_envelope_validates_and_round_trips_schema_aliases() -> None:
    payload = {"program": "pump", "decoded": {"amount": "9007199254740993"}}
    envelope = ProviderEnvelope(
        provider="helius",
        transport="fixture",
        notificationId="fixture-1",
        receivedAt="2026-10-13T00:00:00Z",
        commitment="confirmed",
        slot=0,
        signature=SIGNATURE,
        transactionError={"code": "provider-code"},
        rawPayloadSha256=payload_hash(payload),
        rawPayload=payload,
    )
    serialized = envelope.model_dump(by_alias=True, mode="json", exclude_none=True)
    assert serialized["notification_id"] == "fixture-1"
    assert serialized["raw_payload"]["decoded"]["amount"] == "9007199254740993"
    provider_schema = json.loads(
        (ROOT / "coding-pack/schemas/provider-envelope.schema.json").read_text()
    )
    Draft202012Validator(provider_schema).validate(serialized)
    assert ProviderEnvelope.model_validate(serialized) == envelope


def test_provider_rejects_bad_hash_negative_or_boolean_slot_and_signature() -> None:
    payload = {"x": 1}
    with pytest.raises(ValidationError):
        ProviderEnvelope(
            transport=Transport.FIXTURE,
            notification_id="n",
            received_at=NOW,
            commitment="confirmed",
            slot=-1,
            signature=SIGNATURE,
            raw_payload_sha256=payload_hash(payload),
            raw_payload=payload,
        )
    with pytest.raises(ValidationError):
        ProviderEnvelope(
            transport="fixture",
            notification_id="n",
            received_at=NOW,
            commitment="confirmed",
            slot=True,
            signature="bad",
            raw_payload_sha256="0" * 64,
            raw_payload=payload,
        )
    with pytest.raises(ValidationError):
        ProviderEnvelope(
            transport="fixture",
            notification_id="n",
            received_at=datetime(2026, 10, 13),
            commitment="confirmed",
            slot=1,
            signature=SIGNATURE,
            raw_payload_sha256=payload_hash(payload),
            raw_payload=payload,
        )


def test_instruction_keeps_unknown_instruction_explicit() -> None:
    instruction = DecodedInstruction(
        program_id="unknown-program",
        program_name="unknown",
        instruction_name="unknown",
        instruction_index=0,
        is_inner=False,
        accounts=[{"pubkey": "account"}],
        args={"raw": "kept"},
        matched_filter=False,
    )
    assert instruction.instruction_name == "unknown"
    with pytest.raises(ValidationError):
        DecodedInstruction.model_validate({**instruction.model_dump(), "instruction_index": -1})


def test_canonical_event_preserves_full_domain_and_separates_schema_projection() -> None:
    event = CanonicalSolanaEvent(
        event_key=EVENT_KEY,
        signature=SIGNATURE,
        slot=12,
        block_time="2026-10-13T00:00:00+00:00",
        observed_at="2026-10-13T00:00:01Z",
        commitment="confirmed",
        program="pump",
        program_id="pump-program",
        event_name="TradeEvent",
        event_type="curve_buy",
        event_index=0,
        payload={"sol_amount": "9007199254740993"},
        amounts={"sol": "9007199254740993"},
        reserves={"virtual": "18446744073709551615"},
        decode_status="decoded",
        finality_status="provisional",
        reconciliation_status="pending",
        derivation_version="scout-0.1.0",
    )
    assert event.model_dump(mode="json")["amounts"]["sol"] == "9007199254740993"
    projection = ScoutEventRecord(
        event_key=event.event_key,
        signature=event.signature,
        slot=event.slot,
        block_time=event.block_time,
        program=event.program,
        event_name=event.event_name,
        event_type=event.event_type,
        mint=event.mint,
        user=event.user,
        creator=event.creator,
        bonding_curve=event.bonding_curve,
        pool=event.pool,
        quote_mint=event.quote_mint,
        payload=event.payload,
        decode_status=event.decode_status,
        finality_status=event.finality_status,
        derivation_version=event.derivation_version,
    )
    schema = json.loads((ROOT / "coding-pack/schemas/scout-event.schema.json").read_text())
    Draft202012Validator(schema).validate(
        projection.model_dump(by_alias=True, mode="json", exclude_none=False)
    )
    assert projection.model_dump(mode="json")["slot"] == 12
    with pytest.raises(ValidationError):
        CanonicalSolanaEvent.model_validate({**event.model_dump(), "slot": -1})


def test_evidence_preserves_explicit_unknown_state_without_a_value() -> None:
    evidence = EvidenceResult[str](
        key="freeze_authority",
        state=EvidenceState.NOT_READ,
        source="helius-das",
        observed_at=NOW,
        fresh_until=None,
        value=None,
    )
    assert evidence.value is None
    assert evidence.state is EvidenceState.NOT_READ
    with pytest.raises(ValidationError):
        EvidenceResult[str](key="x", state="observed", source="s", observed_at=datetime(2026, 1, 1))


def test_graph_models_and_edge_evidence_match_contracts() -> None:
    node = GraphNode(
        nodeKey="wallet:abc",
        nodeType="wallet",
        addressOrIdentifier="abc",
        network="mainnet-beta",
        firstSeenAt=NOW,
        lastSeenAt=NOW,
        validationStatus="unknown",
    )
    edge = GraphEdge(
        sourceNodeId=0,
        targetNodeId=1,
        edgeType="funded_wallet",
        direction="directed",
        strength="direct",
        status="active",
        firstObservedAt=NOW,
        lastObservedAt=NOW,
        algorithmVersion="cartographer-0.1.0",
    )
    evidence = GraphEdgeEvidence(
        edgeType="funded_wallet",
        strength="direct",
        evidenceType="shared_funder",
        source="helius_getTransactionsForAddress",
        signature=SIGNATURE,
        slot=0,
        eventKey=EVENT_KEY,
        details={"lamports": "18446744073709551615"},
        observedAt=NOW,
        algorithmVersion="cartographer-0.1.0",
    )
    schema = json.loads((ROOT / "coding-pack/schemas/graph-edge-evidence.schema.json").read_text())
    Draft202012Validator(schema).validate(
        evidence.model_dump(by_alias=True, mode="json", exclude_none=False)
    )
    assert node.node_type.value == "wallet"
    assert edge.strength is GraphEdgeStrength.DIRECT
    assert evidence.details["lamports"] == "18446744073709551615"
    with pytest.raises(ValidationError):
        GraphEdge.model_validate({**edge.model_dump(), "source_node_id": -1})


def test_cartographer_update_is_schema_backed() -> None:
    update = CartographerEvidenceUpdated(
        run_id="run-1",
        mint="mint",
        nodes_added=0,
        edges_added=1,
        edges_retracted=0,
        coverage={"history_complete": False, "wallets_scanned": 1, "transactions_scanned": 2},
        warnings=["history_limit_reached"],
        algorithm_version="cartographer-0.1.0",
    )
    schema = json.loads(
        (ROOT / "coding-pack/schemas/cartographer-evidence.schema.json").read_text()
    )
    Draft202012Validator(schema).validate(update.model_dump(mode="json"))
    assert update.coverage.history_complete is False
