"""Normalize decoded Pump CreateEvent values into canonical Scout events."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import Field, StrictBool, StrictInt, StrictStr, ValidationError, field_validator

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
from app.domain.models import CanonicalSolanaEvent, DecodedEvent, DomainModel

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

    @field_validator("timestamp")
    @classmethod
    def validate_i64_timestamp(cls, value: int) -> int:
        if not _I64_MIN <= value <= _I64_MAX:
            raise ValueError("timestamp must fit a signed 64-bit integer")
        return value


class PumpCreateEventNormalizer:
    """Map one typed decoded Pump CreateEvent without inferring absent fields."""

    derivation_version = "t011"

    def __init__(self, settings: Settings) -> None:
        self._pump_program_id = settings.pump_program_id

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
        if event.decode_status != DecodeStatus.DECODED:
            raise EventNormalizationError("only decoded events can be normalized")
        if type(raw_event_id) is not int or raw_event_id < 0:
            raise EventNormalizationError("raw_event_id must be a non-negative integer")
        if not provider or not signature or type(slot) is not int or slot < 0:
            raise EventNormalizationError("raw event identity metadata is incomplete")
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise EventNormalizationError("observed_at must be timezone-aware")

        try:
            fields = PumpCreateEventFields.model_validate(event.fields)
            block_time = datetime.fromtimestamp(fields.timestamp, tz=UTC)
        except (ValidationError, OverflowError, OSError, ValueError):
            raise EventNormalizationError("Pump CreateEvent fields are malformed") from None

        finalized = commitment == Commitment.FINALIZED
        event_key = canonical_event_key(
            provider,
            signature,
            event.program_id,
            event.event_name,
            event.event_index,
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
            reconciliation_status=(
                ReconciliationStatus.NOT_REQUIRED if finalized else ReconciliationStatus.PENDING
            ),
            derivation_version=self.derivation_version,
            raw_event_id=raw_event_id,
        )
