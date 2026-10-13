"""Normalize decoded Pump events into canonical Scout events."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import (
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
)

from app.config import Settings
from app.domain.enums import (
    Commitment,
    DecodeStatus,
    FinalityStatus,
    ProgramName,
    ReconciliationStatus,
    ScoutEventType,
)
from app.domain.keys import canonical_event_key
from app.domain.models import CanonicalSolanaEvent, DecodedEvent, DomainModel, JsonValue

_U64_MAX = (1 << 64) - 1
_I64_MIN = -(1 << 63)
_I64_MAX = (1 << 63) - 1
_U8_MAX = (1 << 8) - 1
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class EventNormalizationError(ValueError):
    """A decoded event cannot be mapped safely to the supported Scout contract."""


class UnsupportedNormalizationError(EventNormalizationError):
    """No normalization mapping exists for this decoded event."""


def _validate_public_key(value: str) -> str:
    if not 32 <= len(value) <= 44 or any(character not in _BASE58_ALPHABET for character in value):
        raise ValueError("must be a valid 32-byte Base58 public key")
    decoded_number = 0
    for character in value:
        decoded_number = decoded_number * 58 + _BASE58_ALPHABET.index(character)
    decoded_bytes = (decoded_number.bit_length() + 7) // 8
    leading_zero_bytes = len(value) - len(value.lstrip("1"))
    if decoded_bytes + leading_zero_bytes != 32:
        raise ValueError("must be a valid 32-byte Base58 public key")
    return value


def _validate_u64_decimal_string(value: str | None) -> str | None:
    if value is None:
        return None
    if (
        not value
        or not value.isascii()
        or not value.isdecimal()
        or (len(value) > 1 and value[0] == "0")
    ):
        raise ValueError("must be a canonical unsigned decimal string")
    if int(value) > _U64_MAX:
        raise ValueError("must fit an unsigned 64-bit integer")
    return value


class PumpCreateEventFields(DomainModel):
    """Strict current Pump CreateEvent fields; appended fields remain optional."""

    name: StrictStr
    symbol: StrictStr
    uri: StrictStr
    mint: StrictStr
    bonding_curve: StrictStr
    user: StrictStr
    creator: StrictStr
    timestamp: StrictInt
    virtual_token_reserves: StrictStr
    virtual_sol_reserves: StrictStr
    real_token_reserves: StrictStr
    token_total_supply: StrictStr
    token_program: StrictStr | None = None
    is_mayhem_mode: StrictBool | None = None
    is_cashback_enabled: StrictBool | None = None
    quote_mint: StrictStr | None = None
    virtual_quote_reserves: StrictStr | None = None
    creator_fee_bps: StrictStr | None = None
    is_holder_reward: StrictBool | None = None
    depth: StrictInt | None = Field(default=None, ge=0, le=_U8_MAX)

    @field_validator("mint", "bonding_curve", "user", "creator", "token_program", "quote_mint")
    @classmethod
    def validate_public_key(cls, value: str | None) -> str | None:
        return None if value is None else _validate_public_key(value)

    @field_validator(
        "virtual_token_reserves",
        "virtual_sol_reserves",
        "real_token_reserves",
        "token_total_supply",
        "virtual_quote_reserves",
        "creator_fee_bps",
    )
    @classmethod
    def validate_u64_decimal_string(cls, value: str | None) -> str | None:
        return _validate_u64_decimal_string(value)

    @field_validator("timestamp")
    @classmethod
    def validate_i64_timestamp(cls, value: int) -> int:
        if not _I64_MIN <= value <= _I64_MAX:
            raise ValueError("timestamp must fit a signed 64-bit integer")
        return value


class PumpTradeEventFields(DomainModel):
    """Strict Pump TradeEvent fields with forward-compatible appended fields."""

    model_config = ConfigDict(extra="allow")

    mint: StrictStr
    sol_amount: StrictStr
    token_amount: StrictStr
    is_buy: StrictBool
    user: StrictStr
    timestamp: StrictInt
    virtual_sol_reserves: StrictStr
    virtual_token_reserves: StrictStr
    real_sol_reserves: StrictStr
    real_token_reserves: StrictStr
    fee_recipient: StrictStr | None = None
    fee_basis_points: StrictStr | None = None
    fee: StrictStr | None = None
    creator: StrictStr | None = None
    creator_fee_basis_points: StrictStr | None = None
    creator_fee: StrictStr | None = None
    track_volume: StrictBool | None = None
    total_unclaimed_tokens: StrictStr | None = None
    total_claimed_tokens: StrictStr | None = None
    current_sol_volume: StrictStr | None = None
    last_update_timestamp: StrictInt | None = None
    ix_name: StrictStr | None = None
    mayhem_mode: StrictBool | None = None
    cashback_fee_basis_points: StrictStr | None = None
    cashback: StrictStr | None = None
    buyback_fee_basis_points: StrictStr | None = None
    buyback_fee: StrictStr | None = None
    shareholders: list[dict[str, Any]] | None = None
    quote_mint: StrictStr | None = None
    quote_amount: StrictStr | None = None
    virtual_quote_reserves: StrictStr | None = None
    real_quote_reserves: StrictStr | None = None
    holder_rewards_bps: StrictStr | None = None
    holder_rewards: StrictStr | None = None
    creator_fee_unclaimed: StrictStr | None = None

    @field_validator("mint", "user", "fee_recipient", "creator", "quote_mint")
    @classmethod
    def validate_public_key(cls, value: str | None) -> str | None:
        return None if value is None else _validate_public_key(value)

    @field_validator(
        "sol_amount",
        "token_amount",
        "virtual_sol_reserves",
        "virtual_token_reserves",
        "real_sol_reserves",
        "real_token_reserves",
        "fee_basis_points",
        "fee",
        "creator_fee_basis_points",
        "creator_fee",
        "total_unclaimed_tokens",
        "total_claimed_tokens",
        "current_sol_volume",
        "cashback_fee_basis_points",
        "cashback",
        "buyback_fee_basis_points",
        "buyback_fee",
        "quote_amount",
        "virtual_quote_reserves",
        "real_quote_reserves",
        "holder_rewards_bps",
        "holder_rewards",
        "creator_fee_unclaimed",
    )
    @classmethod
    def validate_u64_decimal_string(cls, value: str | None) -> str | None:
        return _validate_u64_decimal_string(value)

    @field_validator("timestamp", "last_update_timestamp")
    @classmethod
    def validate_i64_timestamp(cls, value: int | None) -> int | None:
        if value is not None and not _I64_MIN <= value <= _I64_MAX:
            raise ValueError("timestamp must fit a signed 64-bit integer")
        return value


class _PumpNormalizerBase:
    derivation_version = "t012"

    def __init__(self, settings: Settings) -> None:
        self._pump_program_id = settings.pump_program_id

    def _validate_metadata(
        self,
        event: DecodedEvent,
        *,
        provider: str,
        signature: str,
        slot: int,
        commitment: Commitment,
        observed_at: datetime,
        raw_event_id: int,
    ) -> None:
        if event.program_id != self._pump_program_id:
            raise UnsupportedNormalizationError("no Pump event mapping exists for this event")
        if event.decode_status != DecodeStatus.DECODED:
            raise EventNormalizationError("only decoded events can be normalized")
        if type(raw_event_id) is not int or raw_event_id < 0:
            raise EventNormalizationError("raw_event_id must be a non-negative integer")
        if not provider or not signature or type(slot) is not int or slot < 0:
            raise EventNormalizationError("raw event identity metadata is incomplete")
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise EventNormalizationError("observed_at must be timezone-aware")

    def _event(
        self,
        event: DecodedEvent,
        *,
        provider: str,
        signature: str,
        slot: int,
        commitment: Commitment,
        observed_at: datetime,
        raw_event_id: int,
        event_type: ScoutEventType,
        fields: DomainModel,
        payload: dict[str, Any],
        amounts: dict[str, JsonValue],
        reserves: dict[str, JsonValue],
        creator: str | None,
        quote_mint: str | None,
        bonding_curve: str | None = None,
        pool: str | None = None,
    ) -> CanonicalSolanaEvent:
        block_time = datetime.fromtimestamp(fields.timestamp, tz=UTC)  # type: ignore[attr-defined]
        finalized = commitment == Commitment.FINALIZED
        return CanonicalSolanaEvent(
            event_key=canonical_event_key(
                provider, signature, event.program_id, event.event_name, event.event_index
            ),
            signature=signature,
            slot=slot,
            block_time=block_time,
            observed_at=observed_at,
            commitment=commitment,
            program=ProgramName.PUMP,
            program_id=event.program_id,
            event_name=event.event_name,
            event_type=event_type,
            mint=fields.mint,  # type: ignore[attr-defined]
            user=fields.user,  # type: ignore[attr-defined]
            creator=creator,
            bonding_curve=bonding_curve,
            pool=pool,
            quote_mint=quote_mint,
            instruction_index=event.source_instruction_index,
            event_index=event.event_index,
            payload=payload,
            amounts=amounts,
            reserves=reserves,
            decode_status=DecodeStatus.DECODED,
            finality_status=FinalityStatus.FINALIZED if finalized else FinalityStatus.PROVISIONAL,
            reconciliation_status=ReconciliationStatus.NOT_REQUIRED
            if finalized
            else ReconciliationStatus.PENDING,
            derivation_version=self.derivation_version,
            raw_event_id=raw_event_id,
        )


class _PumpLifecycleFields(DomainModel):
    """Shared strict identity and timestamp fields for Pump lifecycle events."""

    model_config = ConfigDict(extra="allow")

    user: StrictStr
    mint: StrictStr
    bonding_curve: StrictStr
    timestamp: StrictInt
    quote_mint: StrictStr

    @field_validator("user", "mint", "bonding_curve", "quote_mint")
    @classmethod
    def validate_public_key(cls, value: str) -> str:
        return _validate_public_key(value)

    @field_validator("timestamp")
    @classmethod
    def validate_i64_timestamp(cls, value: int) -> int:
        if not _I64_MIN <= value <= _I64_MAX:
            raise ValueError("timestamp must fit a signed 64-bit integer")
        return value


class PumpCompleteEventFields(_PumpLifecycleFields):
    """Pinned Pump CompleteEvent fields."""


class PumpPostCompleteBuyEventFields(_PumpLifecycleFields):
    """Pinned Pump PostCompleteBuyEvent fields."""

    base_out: StrictStr
    quote_in: StrictStr
    fee_basis_points: StrictStr
    fee: StrictStr
    creator_fee_basis_points: StrictStr
    creator_fee: StrictStr
    buyback_fee: StrictStr
    pool_base_reserves_before: StrictStr
    pool_quote_reserves_before: StrictStr
    pool_base_reserves_after: StrictStr
    pool_quote_reserves_after: StrictStr

    @field_validator(
        "base_out",
        "quote_in",
        "fee_basis_points",
        "fee",
        "creator_fee_basis_points",
        "creator_fee",
        "buyback_fee",
        "pool_base_reserves_before",
        "pool_quote_reserves_before",
        "pool_base_reserves_after",
        "pool_quote_reserves_after",
    )
    @classmethod
    def validate_u64_decimal_string(cls, value: str) -> str:
        validated = _validate_u64_decimal_string(value)
        assert validated is not None
        return validated


class PumpMigrationEventFields(_PumpLifecycleFields):
    """Pinned Pump CompletePumpAmmMigrationEvent fields."""

    mint_amount: StrictStr
    sol_amount: StrictStr
    pool_migration_fee: StrictStr
    pool: StrictStr

    @field_validator("pool")
    @classmethod
    def validate_pool(cls, value: str) -> str:
        return _validate_public_key(value)

    @field_validator("mint_amount", "sol_amount", "pool_migration_fee")
    @classmethod
    def validate_u64_decimal_string(cls, value: str) -> str:
        validated = _validate_u64_decimal_string(value)
        assert validated is not None
        return validated


class PumpCompletionMigrationNormalizer(_PumpNormalizerBase):
    """Normalize Pump completion, post-completion buy, and migration events."""

    derivation_version = "t013"

    def normalize(
        self,
        event: DecodedEvent,
        *,
        provider: str,
        signature: str,
        slot: int,
        commitment: Commitment,
        observed_at: datetime,
        raw_event_id: int,
    ) -> CanonicalSolanaEvent:
        supported = {
            "CompleteEvent": PumpCompleteEventFields,
            "PostCompleteBuyEvent": PumpPostCompleteBuyEventFields,
            "CompletePumpAmmMigrationEvent": PumpMigrationEventFields,
        }
        model_type: Any = supported.get(event.event_name)
        if model_type is None or event.program_id != self._pump_program_id:
            raise UnsupportedNormalizationError(
                "no Pump lifecycle event mapping exists for this event"
            )
        self._validate_metadata(
            event,
            provider=provider,
            signature=signature,
            slot=slot,
            commitment=commitment,
            observed_at=observed_at,
            raw_event_id=raw_event_id,
        )
        try:
            fields = model_type.model_validate(event.fields)
            payload = fields.model_dump(mode="json", exclude_unset=True)
        except (ValidationError, OverflowError, OSError, ValueError):
            raise EventNormalizationError("Pump lifecycle event fields are malformed") from None

        payload["event_time"] = fields.timestamp
        amounts: dict[str, JsonValue] = {}
        reserves: dict[str, JsonValue] = {}
        pool: str | None = None
        if event.event_name == "CompleteEvent":
            event_type = ScoutEventType.CURVE_COMPLETE
            payload["curve_complete"] = True
        elif event.event_name == "PostCompleteBuyEvent":
            event_type = ScoutEventType.CURVE_BUY
            payload["event_phase"] = "post_completion"
            payload.update(
                {
                    "base_amount": fields.base_out,
                    "quote_amount": fields.quote_in,
                    "creator_fee_bps": fields.creator_fee_basis_points,
                }
            )
            amounts = {
                "base_amount": fields.base_out,
                "quote_amount": fields.quote_in,
                "fee": fields.fee,
                "creator_fee": fields.creator_fee,
                "buyback_fee": fields.buyback_fee,
            }
            reserves = {
                "pool_base_reserves_before": fields.pool_base_reserves_before,
                "pool_quote_reserves_before": fields.pool_quote_reserves_before,
                "pool_base_reserves_after": fields.pool_base_reserves_after,
                "pool_quote_reserves_after": fields.pool_quote_reserves_after,
            }
        else:
            event_type = ScoutEventType.MIGRATION
            pool = fields.pool
            payload["migrated"] = True
            payload["pool_linkage_status"] = "pending"
            amounts = {
                "mint_amount": fields.mint_amount,
                "sol_amount": fields.sol_amount,
                "pool_migration_fee": fields.pool_migration_fee,
            }

        return self._event(
            event,
            provider=provider,
            signature=signature,
            slot=slot,
            commitment=commitment,
            observed_at=observed_at,
            raw_event_id=raw_event_id,
            event_type=event_type,
            fields=fields,
            payload=payload,
            amounts=amounts,
            reserves=reserves,
            creator=None,
            quote_mint=fields.quote_mint,
            bonding_curve=fields.bonding_curve,
            pool=pool,
        )


class PumpCreateEventNormalizer(_PumpNormalizerBase):
    """Map one typed decoded Pump CreateEvent without inferring absent fields."""

    derivation_version = "t011"

    def normalize(
        self,
        event: DecodedEvent,
        *,
        provider: str,
        signature: str,
        slot: int,
        commitment: Commitment,
        observed_at: datetime,
        raw_event_id: int,
    ) -> CanonicalSolanaEvent:
        if event.event_name != "CreateEvent" or event.program_id != self._pump_program_id:
            raise UnsupportedNormalizationError("no Pump CreateEvent mapping exists for this event")
        self._validate_metadata(
            event,
            provider=provider,
            signature=signature,
            slot=slot,
            commitment=commitment,
            observed_at=observed_at,
            raw_event_id=raw_event_id,
        )
        try:
            fields = PumpCreateEventFields.model_validate(event.fields)
            block_time = datetime.fromtimestamp(fields.timestamp, tz=UTC)
        except (ValidationError, OverflowError, OSError, ValueError):
            raise EventNormalizationError("Pump CreateEvent fields are malformed") from None

        finalized = commitment == Commitment.FINALIZED
        event_key = canonical_event_key(
            provider, signature, event.program_id, event.event_name, event.event_index
        )
        return CanonicalSolanaEvent(
            event_key=event_key,
            signature=signature,
            slot=slot,
            block_time=block_time,
            observed_at=observed_at,
            commitment=commitment,
            program=ProgramName.PUMP,
            program_id=event.program_id,
            event_name=event.event_name,
            event_type=ScoutEventType.TOKEN_CREATED,
            mint=fields.mint,
            user=fields.user,
            creator=fields.creator,
            bonding_curve=fields.bonding_curve,
            quote_mint=fields.quote_mint,
            instruction_index=event.source_instruction_index,
            event_index=event.event_index,
            payload=fields.model_dump(mode="json", exclude_unset=True),
            decode_status=DecodeStatus.DECODED,
            finality_status=FinalityStatus.FINALIZED if finalized else FinalityStatus.PROVISIONAL,
            reconciliation_status=ReconciliationStatus.NOT_REQUIRED
            if finalized
            else ReconciliationStatus.PENDING,
            derivation_version=self.derivation_version,
            raw_event_id=raw_event_id,
        )


class PumpTradeEventNormalizer(_PumpNormalizerBase):
    """Map a typed Pump TradeEvent to a canonical curve buy or sell event."""

    def normalize(
        self,
        event: DecodedEvent,
        *,
        provider: str,
        signature: str,
        slot: int,
        commitment: Commitment,
        observed_at: datetime,
        raw_event_id: int,
    ) -> CanonicalSolanaEvent:
        if event.event_name != "TradeEvent" or event.program_id != self._pump_program_id:
            raise UnsupportedNormalizationError("no Pump TradeEvent mapping exists for this event")
        self._validate_metadata(
            event,
            provider=provider,
            signature=signature,
            slot=slot,
            commitment=commitment,
            observed_at=observed_at,
            raw_event_id=raw_event_id,
        )
        try:
            fields = PumpTradeEventFields.model_validate(event.fields)
            normalized = fields.model_dump(mode="json", exclude_unset=True)
        except (ValidationError, OverflowError, OSError, ValueError):
            raise EventNormalizationError("Pump TradeEvent fields are malformed") from None

        quote_amount = fields.quote_amount or fields.sol_amount
        virtual_quote_reserves = fields.virtual_quote_reserves or fields.virtual_sol_reserves
        real_quote_reserves = fields.real_quote_reserves or fields.real_sol_reserves
        normalized.update(
            {
                "quote_amount": quote_amount,
                "token_amount": fields.token_amount,
                "virtual_quote_reserves": virtual_quote_reserves,
                "virtual_token_reserves": fields.virtual_token_reserves,
                "real_quote_reserves": real_quote_reserves,
                "real_token_reserves": fields.real_token_reserves,
            }
        )
        for normalized_name, raw_name in (
            ("creator_fee_bps", "creator_fee_basis_points"),
            ("holder_rewards", "holder_rewards"),
            ("buyback_fee", "buyback_fee"),
        ):
            value = getattr(fields, raw_name, None)
            if value is not None:
                normalized[normalized_name] = value

        amounts: dict[str, JsonValue] = {
            "quote_amount": quote_amount,
            "token_amount": fields.token_amount,
        }
        for name in (
            "fee",
            "fee_basis_points",
            "creator_fee",
            "creator_fee_bps",
            "holder_rewards",
            "buyback_fee",
        ):
            value = normalized.get(name)
            if isinstance(value, str):
                amounts[name] = value
        reserves: dict[str, JsonValue] = {
            "virtual_quote_reserves": virtual_quote_reserves,
            "virtual_token_reserves": fields.virtual_token_reserves,
            "real_quote_reserves": real_quote_reserves,
            "real_token_reserves": fields.real_token_reserves,
        }
        return self._event(
            event,
            provider=provider,
            signature=signature,
            slot=slot,
            commitment=commitment,
            observed_at=observed_at,
            raw_event_id=raw_event_id,
            event_type=ScoutEventType.CURVE_BUY if fields.is_buy else ScoutEventType.CURVE_SELL,
            fields=fields,
            payload=normalized,
            amounts=amounts,
            reserves=reserves,
            creator=fields.creator,
            quote_mint=fields.quote_mint,
        )
