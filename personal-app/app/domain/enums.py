"""Closed vocabulary used by the provider, Scout, and Cartographer domains."""

from __future__ import annotations

from enum import StrEnum


class Transport(StrEnum):
    PARSED_STREAMS = "parsed_streams"
    LASERSTREAM = "laserstream"
    LOGS_SUBSCRIBE = "logs_subscribe"
    PARSED_EVENTS = "parsed_events"
    FIXTURE = "fixture"


class Commitment(StrEnum):
    PROCESSED = "processed"
    CONFIRMED = "confirmed"
    FINALIZED = "finalized"


class ProgramName(StrEnum):
    PUMP = "pump"
    PUMP_AMM = "pump_amm"
    UNKNOWN = "unknown"


class DecodeStatus(StrEnum):
    DECODED = "decoded"
    RAW_ONLY = "raw_only"
    FAILED = "failed"


class FinalityStatus(StrEnum):
    PROVISIONAL = "provisional"
    FINALIZED = "finalized"
    REORGED_OR_CHANGED = "reorged_or_changed"


class ReconciliationStatus(StrEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    MATCHED = "matched"
    CHANGED = "changed"
    MISSING = "missing"


class EvidenceState(StrEnum):
    OBSERVED = "observed"
    NOT_READ = "not_read"
    NOT_APPLICABLE = "not_applicable"
    ERROR = "error"
    STALE = "stale"


class Network(StrEnum):
    MAINNET_BETA = "mainnet-beta"
    DEVNET = "devnet"
    TESTNET = "testnet"


class NodeValidationStatus(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    UNKNOWN = "unknown"


class GraphNodeType(StrEnum):
    WALLET = "wallet"
    TOKEN_MINT = "token_mint"
    TOKEN_ACCOUNT = "token_account"
    BONDING_CURVE = "bonding_curve"
    POOL = "pool"
    LP_MINT = "lp_mint"
    TRANSACTION = "transaction"
    PROGRAM = "program"
    METADATA_URI = "metadata_uri"
    BUNDLE = "bundle"


class GraphEdgeType(StrEnum):
    CREATED_TOKEN = "created_token"
    USER_CREATED_TOKEN = "user_created_token"
    CREATOR_DECLARED_FOR_TOKEN = "creator_declared_for_token"
    OWNS_TOKEN_ACCOUNT = "owns_token_account"
    TRADED_TOKEN = "traded_token"
    FUNDED_WALLET = "funded_wallet"
    RECEIVED_SOL_FROM = "received_sol_from"
    RECEIVED_TOKEN_FROM = "received_token_from"
    SHARED_TRANSACTION = "shared_transaction"
    SAME_SLOT_ACTIVITY = "same_slot_activity"
    SAME_BUNDLE_ACTIVITY = "same_bundle_activity"
    SHARES_COUNTERPARTY = "shares_counterparty"
    CONTROLS_BONDING_CURVE = "controls_bonding_curve"
    ASSOCIATED_WITH_POOL = "associated_with_pool"
    COIN_CREATOR_FOR_POOL = "coin_creator_for_pool"
    SAME_METADATA_URI = "same_metadata_uri"
    FEE_RECIPIENT_FOR = "fee_recipient_for"


class GraphEdgeDirection(StrEnum):
    DIRECTED = "directed"
    UNDIRECTED = "undirected"


class GraphEdgeStrength(StrEnum):
    DIRECT = "direct"
    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"
    UNKNOWN = "unknown"


class GraphEdgeStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    RETRACTED = "retracted"
    NEEDS_REVIEW = "needs_review"


class ScoutEventType(StrEnum):
    TOKEN_CREATED = "token_created"
    CURVE_BUY = "curve_buy"
    CURVE_SELL = "curve_sell"
    CURVE_COMPLETE = "curve_complete"
    MIGRATION = "migration"
    POOL_CREATED = "pool_created"
    POOL_BUY = "pool_buy"
    POOL_SELL = "pool_sell"
