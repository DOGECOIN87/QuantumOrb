"""Provider-neutral decoder contract and local fixture adapter."""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

from pydantic import Field, ValidationError, field_validator, model_validator

from app.config import Settings
from app.db.catalog import CatalogSnapshotInput, load_pinned_catalog
from app.db.decode_outcomes import DecodeOutcomeRepository
from app.db.raw_journal import RawEventRecord, RawJournal
from app.domain.enums import DecodeStatus
from app.domain.models import (
    DecodedEvent,
    DecodedInstruction,
    DomainModel,
    NonNegativeInt,
    ProviderEnvelope,
)


class DecoderConfigurationError(ValueError):
    """The local pinned catalog cannot safely initialize a decoder."""


class DecodePipelineError(ValueError):
    """A decoder returned an outcome for a different raw event."""


class DecoderInput(DomainModel):
    """Provider-independent view of an already persisted raw event."""

    raw_event_id: NonNegativeInt
    transport: str
    payload_json: str


class DecodeResult(DomainModel):
    """Low-level decode outcome, deliberately separate from Scout classification."""

    raw_event_id: NonNegativeInt
    status: DecodeStatus
    instructions: list[DecodedInstruction] = Field(default_factory=list)
    events: list[DecodedEvent] = Field(default_factory=list)
    unknown_event_name: str | None = None
    safe_error_code: str | None = None
    safe_error_message: str | None = None

    @field_validator("unknown_event_name")
    @classmethod
    def validate_unknown_event_name(cls, value: str | None) -> str | None:
        if value is not None and (not value or value != value.strip() or len(value) > 128):
            raise ValueError("unknown event name is invalid")
        return value

    @field_validator("safe_error_code")
    @classmethod
    def validate_safe_error_code(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value) is None:
            raise ValueError("safe error code is invalid")
        return value

    @field_validator("safe_error_message")
    @classmethod
    def validate_safe_error_message(cls, value: str | None) -> str | None:
        if value is not None and (not value or len(value) > 256 or "\n" in value or "\r" in value):
            raise ValueError("safe error message is invalid")
        return value

    @model_validator(mode="after")
    def validate_outcome_shape(self) -> DecodeResult:
        if self.status == DecodeStatus.DECODED:
            if not self.events and not self.instructions:
                raise ValueError("decoded result must contain a typed instruction or event")
            if self.safe_error_code is not None or self.safe_error_message is not None:
                raise ValueError("decoded result cannot carry an error")
            if self.unknown_event_name is not None:
                raise ValueError("decoded result cannot be an unknown event")
        else:
            if self.events or self.instructions:
                raise ValueError("raw-only and failed results cannot contain decoded output")
            if self.safe_error_code is None or self.safe_error_message is None:
                raise ValueError("raw-only and failed results require a safe diagnostic")
            if self.status == DecodeStatus.FAILED and self.unknown_event_name is not None:
                raise ValueError("failed result cannot be an unknown event")
        return self


class Decoder(Protocol):
    """Provider-neutral interface that consumes only persisted raw-event data."""

    def decode(self, event: DecoderInput) -> DecodeResult: ...


class FixtureDecoder:
    """Adapt explicit fixture-shaped events using the pinned local program IDLs."""

    _ERROR_MESSAGES = {
        "unknown_program": "program is not present in the pinned catalog",
        "unknown_event": "event is not present in the pinned program IDL",
        "unsupported_transport": "transport has no fixture decoder",
        "malformed_event": "fixture event does not match the decoder input contract",
        "program_id_mismatch": "fixture program address does not match the pinned catalog",
        "invalid_payload": "stored payload is not a JSON object",
    }

    def __init__(
        self,
        settings: Settings,
        *,
        snapshots: tuple[CatalogSnapshotInput, ...] | None = None,
    ) -> None:
        pinned = load_pinned_catalog(settings) if snapshots is None else snapshots
        programs: dict[str, tuple[str, frozenset[str]]] = {}
        for snapshot in pinned:
            if snapshot.program_name not in {"pump", "pump_amm"}:
                raise DecoderConfigurationError("pinned catalog contains an unsupported program")
            try:
                payload: Any = json.loads(snapshot.payload_json)
            except json.JSONDecodeError:
                raise DecoderConfigurationError("pinned catalog payload is invalid") from None
            names = self._event_names(payload)
            if not names or snapshot.program_name in programs:
                raise DecoderConfigurationError("pinned catalog event list is incomplete")
            programs[snapshot.program_name] = (snapshot.program_id, names)
        if set(programs) != {"pump", "pump_amm"}:
            raise DecoderConfigurationError("pinned catalog must include Pump and PumpSwap")
        self._programs = programs

    @staticmethod
    def _event_names(payload: Any) -> frozenset[str]:
        if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
            return frozenset()
        names = {
            event["name"]
            for event in payload["events"]
            if isinstance(event, dict) and isinstance(event.get("name"), str) and event["name"]
        }
        return frozenset(names)

    def _result(
        self,
        event: DecoderInput,
        status: DecodeStatus,
        error_code: str,
        *,
        unknown_event_name: str | None = None,
    ) -> DecodeResult:
        return DecodeResult(
            raw_event_id=event.raw_event_id,
            status=status,
            unknown_event_name=unknown_event_name,
            safe_error_code=error_code,
            safe_error_message=self._ERROR_MESSAGES[error_code],
        )

    def decode(self, event: DecoderInput) -> DecodeResult:
        """Decode an already-journaled fixture event without assigning Scout event types."""
        if event.transport != "fixture":
            return self._result(event, DecodeStatus.RAW_ONLY, "unsupported_transport")
        try:
            payload: Any = json.loads(event.payload_json)
        except (TypeError, json.JSONDecodeError):
            return self._result(event, DecodeStatus.FAILED, "invalid_payload")
        if not isinstance(payload, dict):
            return self._result(event, DecodeStatus.FAILED, "invalid_payload")

        program = payload.get("program")
        if not isinstance(program, str) or not program:
            return self._result(event, DecodeStatus.FAILED, "malformed_event")
        program_info = self._programs.get(program)
        if program_info is None:
            return self._result(event, DecodeStatus.RAW_ONLY, "unknown_program")
        program_id, event_names = program_info

        supplied_program_id = payload.get("programId", payload.get("program_id"))
        if supplied_program_id is not None and supplied_program_id != program_id:
            return self._result(event, DecodeStatus.FAILED, "program_id_mismatch")

        event_name = payload.get("eventName", payload.get("event_name"))
        if not isinstance(event_name, str) or not event_name or event_name != event_name.strip():
            return self._result(event, DecodeStatus.FAILED, "malformed_event")
        if event_name not in event_names:
            return self._result(
                event,
                DecodeStatus.RAW_ONLY,
                "unknown_event",
                unknown_event_name=event_name,
            )

        event_index = payload.get("eventIndex", payload.get("event_index"))
        fields = payload.get("decoded")
        source_instruction_index = payload.get(
            "sourceInstructionIndex", payload.get("source_instruction_index")
        )
        if (
            type(event_index) is not int
            or event_index < 0
            or not isinstance(fields, dict)
            or (
                source_instruction_index is not None
                and (type(source_instruction_index) is not int or source_instruction_index < 0)
            )
        ):
            return self._result(event, DecodeStatus.FAILED, "malformed_event")
        try:
            decoded_event = DecodedEvent(
                event_name=event_name,
                program_id=program_id,
                event_index=event_index,
                fields=fields,
                source_instruction_index=source_instruction_index,
                decode_status=DecodeStatus.DECODED,
            )
        except ValidationError:
            return self._result(event, DecodeStatus.FAILED, "malformed_event")
        return DecodeResult(
            raw_event_id=event.raw_event_id,
            status=DecodeStatus.DECODED,
            events=[decoded_event],
        )


class DecodePipeline:
    """Persist the immutable provider envelope before invoking a decoder."""

    def __init__(
        self,
        raw_journal: RawJournal,
        decoder: Decoder,
        outcome_repository: DecodeOutcomeRepository,
    ) -> None:
        self._raw_journal = raw_journal
        self._decoder = decoder
        self._outcome_repository = outcome_repository

    def process(self, envelope: ProviderEnvelope) -> DecodeResult:
        raw_event: RawEventRecord = self._raw_journal.insert(envelope)
        result = self._decoder.decode(
            DecoderInput(
                raw_event_id=raw_event.id,
                transport=raw_event.transport,
                payload_json=raw_event.payload_json,
            )
        )
        if result.raw_event_id != raw_event.id:
            raise DecodePipelineError("decoder outcome references a different raw event")
        self._outcome_repository.record_outcome(
            raw_event_id=raw_event.id,
            status=result.status,
            safe_error_code=result.safe_error_code,
            safe_error_message=result.safe_error_message,
        )
        return result
