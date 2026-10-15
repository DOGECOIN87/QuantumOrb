"""Typed, provider-independent domain records for T003.

Schema-backed records use snake_case aliases so their serialized form matches the
JSON contracts, while Python attributes follow the TypeScript-style domain names
where that distinction is useful.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Generic, TypeAlias, TypeVar

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic.types import JsonValue as PydanticJsonValue

from app.domain.enums import (
    Commitment,
    DecodeStatus,
    EvidenceState,
    FinalityStatus,
    GraphEdgeDirection,
    GraphEdgeStatus,
    GraphEdgeStrength,
    GraphEdgeType,
    GraphNodeType,
    Network,
    NodeValidationStatus,
    ProgramName,
    ReconciliationStatus,
    ScoutEventType,
    Transport,
)

JsonValue: TypeAlias = PydanticJsonValue
JsonObject: TypeAlias = dict[str, JsonValue]
DecimalString = Annotated[str, StringConstraints(pattern=r"^(0|[1-9][0-9]*)$")]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class DomainModel(BaseModel):
    """Common strictness and safe extension policy for domain records."""

    model_config = ConfigDict(
        extra="forbid",
        validate_by_alias=True,
        validate_by_name=True,
        str_strip_whitespace=False,
        validate_assignment=True,
    )


class UtcModel(DomainModel):
    @field_validator(
        "received_at",
        "observed_at",
        "block_time",
        "fresh_until",
        "expires_at",
        mode="before",
        check_fields=False,
    )
    @classmethod
    def require_utc_datetime(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, datetime):
            value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime must be timezone-aware")
        return value.astimezone(UTC)


def _non_empty(value: str) -> str:
    if not value:
        raise ValueError("must not be empty")
    return value


def _base58_decoded_size(value: str) -> int:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    number = 0
    for character in value:
        if character not in alphabet:
            raise ValueError("must be Base58")
        number = number * 58 + alphabet.index(character)
    return (number.bit_length() + 7) // 8 + len(value) - len(value.lstrip("1"))


def _signature(value: str) -> str:
    if not 32 <= len(value) <= 88 or _base58_decoded_size(value) != 64:
        raise ValueError("must be a 64-byte Base58 Solana signature")
    return value


class ProviderEnvelope(UtcModel):
    provider: str = Field("helius", pattern=r"^helius$")
    transport: Transport
    notification_id: str = Field(validation_alias=AliasChoices("notification_id", "notificationId"))
    received_at: datetime = Field(validation_alias=AliasChoices("received_at", "receivedAt"))
    commitment: Commitment
    slot: NonNegativeInt
    signature: str
    transaction_error: JsonValue | None = Field(
        default=None, validation_alias=AliasChoices("transaction_error", "transactionError")
    )
    raw_payload_sha256: Sha256 = Field(
        validation_alias=AliasChoices("raw_payload_sha256", "rawPayloadSha256")
    )
    idl_revision: str | None = Field(
        default=None, validation_alias=AliasChoices("idl_revision", "idlRevision")
    )
    raw_payload: JsonObject = Field(validation_alias=AliasChoices("raw_payload", "rawPayload"))

    _validate_notification = field_validator("notification_id")(_non_empty)
    _validate_signature = field_validator("signature")(_signature)

    @model_validator(mode="after")
    def validate_payload_hash(self) -> ProviderEnvelope:
        serialized = json.dumps(self.raw_payload, sort_keys=True, separators=(",", ":"))
        expected = hashlib.sha256(serialized.encode()).hexdigest()
        if self.raw_payload_sha256 != expected:
            raise ValueError("raw_payload_sha256 does not match canonical raw_payload JSON")
        return self


class DecodedAccount(DomainModel):
    name: str | None = None
    pubkey: str
    is_signer: StrictBool | None = Field(
        default=None, validation_alias=AliasChoices("is_signer", "isSigner")
    )
    is_writable: StrictBool | None = Field(
        default=None, validation_alias=AliasChoices("is_writable", "isWritable")
    )

    _validate_pubkey = field_validator("pubkey")(_non_empty)


class DecodedInstruction(DomainModel):
    program_id: str = Field(validation_alias=AliasChoices("program_id", "programId"))
    program_name: ProgramName
    instruction_name: str = Field(
        validation_alias=AliasChoices("instruction_name", "instructionName")
    )
    instruction_index: NonNegativeInt = Field(
        validation_alias=AliasChoices("instruction_index", "instructionIndex")
    )
    is_inner: StrictBool = Field(validation_alias=AliasChoices("is_inner", "isInner"))
    accounts: list[DecodedAccount]
    args: JsonObject
    matched_filter: StrictBool = Field(
        validation_alias=AliasChoices("matched_filter", "matchedFilter")
    )

    _validate_program_id = field_validator("program_id")(_non_empty)
    _validate_instruction_name = field_validator("instruction_name")(_non_empty)


class DecodedEvent(UtcModel):
    event_name: str = Field(validation_alias=AliasChoices("event_name", "eventName"))
    program_id: str = Field(validation_alias=AliasChoices("program_id", "programId"))
    event_index: NonNegativeInt = Field(validation_alias=AliasChoices("event_index", "eventIndex"))
    fields: JsonObject
    source_instruction_index: NonNegativeInt | None = Field(
        default=None,
        validation_alias=AliasChoices("source_instruction_index", "sourceInstructionIndex"),
    )
    decode_status: DecodeStatus = Field(
        validation_alias=AliasChoices("decode_status", "decodeStatus")
    )

    _validate_event_name = field_validator("event_name", "program_id")(_non_empty)


class CanonicalSolanaEvent(UtcModel):
    event_key: Sha256 = Field(validation_alias=AliasChoices("event_key", "eventKey"))
    signature: str
    slot: NonNegativeInt
    block_time: datetime | None = Field(
        default=None, validation_alias=AliasChoices("block_time", "blockTime")
    )
    observed_at: datetime = Field(validation_alias=AliasChoices("observed_at", "observedAt"))
    commitment: Commitment
    program: ProgramName
    program_id: str = Field(validation_alias=AliasChoices("program_id", "programId"))
    event_name: str = Field(validation_alias=AliasChoices("event_name", "eventName"))
    event_type: ScoutEventType = Field(validation_alias=AliasChoices("event_type", "eventType"))
    mint: str | None = None
    user: str | None = None
    creator: str | None = None
    bonding_curve: str | None = Field(
        default=None, validation_alias=AliasChoices("bonding_curve", "bondingCurve")
    )
    pool: str | None = None
    quote_mint: str | None = Field(
        default=None, validation_alias=AliasChoices("quote_mint", "quoteMint")
    )
    transaction_index: NonNegativeInt | None = Field(
        default=None, validation_alias=AliasChoices("transaction_index", "transactionIndex")
    )
    instruction_index: NonNegativeInt | None = Field(
        default=None, validation_alias=AliasChoices("instruction_index", "instructionIndex")
    )
    event_index: NonNegativeInt = Field(
        default=0, validation_alias=AliasChoices("event_index", "eventIndex")
    )
    payload: JsonObject
    amounts: JsonObject | None = None
    reserves: JsonObject | None = None
    decode_status: DecodeStatus = Field(
        validation_alias=AliasChoices("decode_status", "decodeStatus")
    )
    finality_status: FinalityStatus = Field(
        validation_alias=AliasChoices("finality_status", "finalityStatus")
    )
    reconciliation_status: ReconciliationStatus = Field(
        validation_alias=AliasChoices("reconciliation_status", "reconciliationStatus")
    )
    derivation_version: str = Field(
        validation_alias=AliasChoices("derivation_version", "derivationVersion")
    )
    raw_event_id: NonNegativeInt | None = Field(
        default=None, validation_alias=AliasChoices("raw_event_id", "rawEventId")
    )

    _validate_signature = field_validator("signature")(_signature)
    _validate_strings = field_validator("program_id", "event_name", "derivation_version")(
        _non_empty
    )

    @field_validator("block_time", mode="before")
    @classmethod
    def require_utc_block_time(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, datetime):
            value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime must be timezone-aware")
        return value.astimezone(UTC)


class ScoutEventRecord(UtcModel):
    """The intentionally narrower record accepted by scout-event.schema.json."""

    event_key: Sha256 = Field(validation_alias=AliasChoices("event_key", "eventKey"))
    signature: str
    slot: NonNegativeInt
    block_time: datetime | None = Field(
        default=None, validation_alias=AliasChoices("block_time", "blockTime")
    )
    program: ProgramName
    event_name: str = Field(validation_alias=AliasChoices("event_name", "eventName"))
    event_type: ScoutEventType = Field(validation_alias=AliasChoices("event_type", "eventType"))
    mint: str | None = None
    user: str | None = None
    creator: str | None = None
    bonding_curve: str | None = Field(
        default=None, validation_alias=AliasChoices("bonding_curve", "bondingCurve")
    )
    pool: str | None = None
    quote_mint: str | None = Field(
        default=None, validation_alias=AliasChoices("quote_mint", "quoteMint")
    )
    payload: JsonObject = Field(default_factory=dict)
    decode_status: DecodeStatus = Field(
        validation_alias=AliasChoices("decode_status", "decodeStatus")
    )
    finality_status: FinalityStatus = Field(
        validation_alias=AliasChoices("finality_status", "finalityStatus")
    )
    derivation_version: str = Field(
        validation_alias=AliasChoices("derivation_version", "derivationVersion")
    )

    _validate_signature = field_validator("signature")(_signature)


T = TypeVar("T")


class EvidenceResult(UtcModel, Generic[T]):
    key: str
    state: EvidenceState
    value: T | None = None
    source: str
    source_endpoint: str | None = Field(
        default=None, validation_alias=AliasChoices("source_endpoint", "sourceEndpoint")
    )
    source_signature: str | None = Field(
        default=None, validation_alias=AliasChoices("source_signature", "sourceSignature")
    )
    observed_at: datetime = Field(validation_alias=AliasChoices("observed_at", "observedAt"))
    fresh_until: datetime | None = Field(
        default=None, validation_alias=AliasChoices("fresh_until", "freshUntil")
    )
    source_slot: NonNegativeInt | None = Field(
        default=None, validation_alias=AliasChoices("source_slot", "sourceSlot")
    )
    safe_error_code: str | None = Field(
        default=None, validation_alias=AliasChoices("safe_error_code", "safeErrorCode")
    )
    safe_error_message: str | None = Field(
        default=None, validation_alias=AliasChoices("safe_error_message", "safeErrorMessage")
    )
    algorithm_version: str | None = Field(
        default=None, validation_alias=AliasChoices("algorithm_version", "algorithmVersion")
    )


class GraphNode(DomainModel):
    id: NonNegativeInt | None = None
    node_key: str = Field(validation_alias=AliasChoices("node_key", "nodeKey"))
    node_type: GraphNodeType = Field(validation_alias=AliasChoices("node_type", "nodeType"))
    address_or_identifier: str = Field(
        validation_alias=AliasChoices("address_or_identifier", "addressOrIdentifier")
    )
    network: Network
    first_seen_at: datetime = Field(validation_alias=AliasChoices("first_seen_at", "firstSeenAt"))
    last_seen_at: datetime = Field(validation_alias=AliasChoices("last_seen_at", "lastSeenAt"))
    metadata: JsonObject | None = None
    validation_status: NodeValidationStatus = Field(
        validation_alias=AliasChoices("validation_status", "validationStatus")
    )

    _validate_node_strings = field_validator("node_key", "address_or_identifier")(_non_empty)

    @field_validator("first_seen_at", "last_seen_at", mode="before")
    @classmethod
    def utc_times(cls, value: object) -> object:
        if not isinstance(value, datetime):
            value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime must be timezone-aware")
        return value.astimezone(UTC)


class GraphEdge(DomainModel):
    id: NonNegativeInt | None = None
    source_node_id: NonNegativeInt = Field(
        validation_alias=AliasChoices("source_node_id", "sourceNodeId")
    )
    target_node_id: NonNegativeInt = Field(
        validation_alias=AliasChoices("target_node_id", "targetNodeId")
    )
    edge_type: GraphEdgeType = Field(validation_alias=AliasChoices("edge_type", "edgeType"))
    direction: GraphEdgeDirection
    strength: GraphEdgeStrength
    status: GraphEdgeStatus
    first_observed_at: datetime = Field(
        validation_alias=AliasChoices("first_observed_at", "firstObservedAt")
    )
    last_observed_at: datetime = Field(
        validation_alias=AliasChoices("last_observed_at", "lastObservedAt")
    )
    algorithm_version: str = Field(
        validation_alias=AliasChoices("algorithm_version", "algorithmVersion")
    )

    @field_validator("first_observed_at", "last_observed_at", mode="before")
    @classmethod
    def utc_times(cls, value: object) -> object:
        if not isinstance(value, datetime):
            value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime must be timezone-aware")
        return value.astimezone(UTC)


class GraphEdgeEvidence(UtcModel):
    edge_type: GraphEdgeType = Field(validation_alias=AliasChoices("edge_type", "edgeType"))
    strength: GraphEdgeStrength
    evidence_type: str = Field(validation_alias=AliasChoices("evidence_type", "evidenceType"))
    source: str
    signature: str | None = None
    slot: NonNegativeInt | None = None
    event_key: Sha256 | None = Field(
        default=None, validation_alias=AliasChoices("event_key", "eventKey")
    )
    details: JsonObject = Field(default_factory=dict)
    observed_at: datetime = Field(validation_alias=AliasChoices("observed_at", "observedAt"))
    expires_at: datetime | None = Field(
        default=None, validation_alias=AliasChoices("expires_at", "expiresAt")
    )
    algorithm_version: str = Field(
        validation_alias=AliasChoices("algorithm_version", "algorithmVersion")
    )

    @field_validator("signature")
    @classmethod
    def validate_optional_signature(cls, value: str | None) -> str | None:
        return None if value is None else _signature(value)


class CartographerCoverage(DomainModel):
    history_complete: StrictBool
    wallets_scanned: NonNegativeInt
    transactions_scanned: NonNegativeInt


class CartographerEvidenceUpdated(DomainModel):
    type: str = Field("CartographerEvidenceUpdated", pattern=r"^CartographerEvidenceUpdated$")
    schema_version: str = Field("0.1", pattern=r"^0\.1$")
    run_id: str
    mint: str
    nodes_added: NonNegativeInt
    edges_added: NonNegativeInt
    edges_retracted: NonNegativeInt
    coverage: CartographerCoverage
    warnings: list[str] = Field(default_factory=list)
    algorithm_version: str
