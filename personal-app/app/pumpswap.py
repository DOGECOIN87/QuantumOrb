"""Offline PumpSwap normalization against the verified, pinned IDL."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from app.config import Settings
from app.db.catalog import load_pinned_catalog
from app.domain.enums import (
    Commitment,
    DecodeStatus,
    FinalityStatus,
    ProgramName,
    ReconciliationStatus,
    ScoutEventType,
)
from app.domain.keys import canonical_event_key
from app.domain.models import CanonicalSolanaEvent, DecodedEvent, JsonValue
from app.normalization import (
    EventNormalizationError,
    UnsupportedNormalizationError,
    _validate_public_key,
)


class PumpSwapEventNormalizer:
    """Preserve available fields; never infer trade mints from a pool address."""

    derivation_version = "t014"
    _types = {
        "CreatePoolEvent": ScoutEventType.POOL_CREATED,
        "BuyEvent": ScoutEventType.POOL_BUY,
        "SellEvent": ScoutEventType.POOL_SELL,
    }

    def __init__(self, settings: Settings) -> None:
        self._program_id = settings.pump_amm_program_id
        snapshot = next(s for s in load_pinned_catalog(settings) if s.program_name == "pump_amm")
        idl = json.loads(snapshot.payload_json)
        self._fields: dict[str, dict[str, str]] = {
            item["name"]: {f["name"]: f["type"] for f in item["type"]["fields"]}
            for item in idl["types"]
            if item["name"] in self._types
        }

    @staticmethod
    def _validate(value: Any, kind: str) -> None:
        if kind == "pubkey":
            if not isinstance(value, str):
                raise ValueError("invalid address")
            _validate_public_key(value)
        elif kind == "bool":
            if type(value) is not bool:
                raise ValueError("invalid boolean")
        elif kind == "string":
            if not isinstance(value, str):
                raise ValueError("invalid string")
        elif kind in {"u64", "i128"}:
            pattern = r"(?:0|[1-9][0-9]*)" if kind == "u64" else r"(?:0|-?[1-9][0-9]*)"
            if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
                raise ValueError("invalid decimal string")
            number = int(value)
            lower, upper = (0, (1 << 64) - 1) if kind == "u64" else (-(1 << 127), (1 << 127) - 1)
            if not lower <= number <= upper:
                raise ValueError("integer out of range")
        elif kind in {"u8", "u16", "i64"}:
            lower, upper = {"u8": (0, 255), "u16": (0, 65535), "i64": (-(1 << 63), (1 << 63) - 1)}[
                kind
            ]
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError("integer out of range")
        else:
            raise ValueError("unsupported IDL field type")

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
        if event.program_id != self._program_id or event.event_name not in self._types:
            raise UnsupportedNormalizationError("no PumpSwap mapping exists for this event")
        if (
            event.decode_status != DecodeStatus.DECODED
            or type(raw_event_id) is not int
            or raw_event_id < 0
            or not provider
            or not signature
            or type(slot) is not int
            or slot < 0
            or observed_at.tzinfo is None
            or observed_at.utcoffset() is None
        ):
            raise EventNormalizationError("PumpSwap event metadata is invalid")
        pool_created = event.event_name == "CreatePoolEvent"
        required = {"timestamp", "pool"}
        if pool_created:
            required |= {
                "index",
                "creator",
                "base_mint",
                "quote_mint",
                "lp_mint",
                "pool_base_amount",
                "pool_quote_amount",
                "initial_liquidity",
                "minimum_liquidity",
            }
        else:
            required |= {
                "user",
                "pool_base_token_reserves",
                "pool_quote_token_reserves",
                "virtual_quote_reserves",
            }
            required |= (
                {"base_amount_out", "quote_amount_in"}
                if event.event_name == "BuyEvent"
                else {"base_amount_in", "quote_amount_out"}
            )
        fields: dict[str, Any] = dict(event.fields)
        try:
            if not required <= fields.keys():
                raise ValueError("missing required fields")
            for name, value in fields.items():
                kind = self._fields[event.event_name].get(name)
                if kind is not None:
                    self._validate(value, kind)
            block_time = datetime.fromtimestamp(fields["timestamp"], tz=UTC)
        except (ValueError, TypeError, OverflowError, OSError):
            raise EventNormalizationError("PumpSwap event fields are malformed") from None
        amounts: dict[str, JsonValue] = {}
        reserves: dict[str, JsonValue] = {}
        for name, kind in self._fields[event.event_name].items():
            if name in fields and kind in {"u64", "i128"}:
                target = (
                    reserves
                    if "reserves" in name or name in {"pool_base_amount", "pool_quote_amount"}
                    else amounts
                )
                target[name] = fields[name]
        if not pool_created:
            buy = event.event_name == "BuyEvent"
            fields["side"] = "buy" if buy else "sell"
            for amount_name, source in (
                ("base_amount", "base_amount_out" if buy else "base_amount_in"),
                ("quote_amount", "quote_amount_in" if buy else "quote_amount_out"),
                ("user_quote_amount", "user_quote_amount_in" if buy else "user_quote_amount_out"),
            ):
                if source in fields:
                    fields[amount_name] = fields[source]
                    amounts[amount_name] = fields[source]
            effective = str(
                int(fields["pool_quote_token_reserves"]) + int(fields["virtual_quote_reserves"])
            )
            fields["effective_quote_reserves"] = effective
            reserves["effective_quote_reserves"] = effective
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
            program=ProgramName.PUMP_AMM,
            program_id=event.program_id,
            event_name=event.event_name,
            event_type=self._types[event.event_name],
            mint=fields.get("base_mint") if pool_created else None,
            user=fields.get("creator") if pool_created else fields["user"],
            creator=fields.get("coin_creator"),
            pool=fields["pool"],
            quote_mint=fields.get("quote_mint") if pool_created else None,
            instruction_index=event.source_instruction_index,
            event_index=event.event_index,
            payload=fields,
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
