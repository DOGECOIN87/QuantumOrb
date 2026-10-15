"""Offline journal replay through the same decoders and Scout normalizers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from app.config import Settings
from app.db.canonical_events import CanonicalEventRepository
from app.db.decode_outcomes import DecodeOutcomeRepository
from app.db.lifecycles import LifecycleRepository
from app.db.models import raw_events
from app.db.raw_journal import RawJournal, SessionSource
from app.decoders import DecodeResult, DecoderInput, FixtureDecoder
from app.domain.enums import Commitment, DecodeStatus
from app.integrations.fixture_transport import FixtureTransport
from app.integrations.raw_logs_decoder import RawLogsDecoder
from app.normalization import (
    PumpCompletionMigrationNormalizer,
    PumpCreateEventNormalizer,
    PumpTradeEventNormalizer,
    UnsupportedNormalizationError,
)
from app.pumpswap import PumpSwapEventNormalizer


class ReplayError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("replay could not be completed")


@dataclass
class ReplayReport:
    raw_read: int = 0
    decoded: int = 0
    raw_only: int = 0
    failed: int = 0
    canonical_processed: int = 0
    last_raw_id: int | None = None
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)


class ReplayService:
    def __init__(self, source: SessionSource, settings: Settings) -> None:
        self._source = source
        self._settings = settings
        self._fixture = FixtureDecoder(settings)
        self._normalizers = (
            PumpCreateEventNormalizer(settings),
            PumpTradeEventNormalizer(settings),
            PumpCompletionMigrationNormalizer(settings),
            PumpSwapEventNormalizer(settings),
        )

    def apply_decoded(self, result: DecodeResult) -> int:
        """Common live/replay consumer; the referenced raw row already exists."""
        if result.status != DecodeStatus.DECODED:
            return 0
        session, owned = RawJournal(self._source)._session()
        try:
            row = session.execute(
                select(raw_events).where(raw_events.c.id == result.raw_event_id)
            ).one()
            raw = RawJournal._record(row)
        finally:
            if owned:
                session.close()
        if hashlib.sha256(raw.payload_json.encode()).hexdigest() != raw.payload_sha256:
            raise ReplayError("replay_payload_hash_mismatch")
        if raw.signature is None or raw.slot is None or raw.commitment is None:
            raise ReplayError("replay_context_unavailable")
        count = 0
        for event in result.events:
            normalized = None
            for normalizer in self._normalizers:
                try:
                    normalized = normalizer.normalize(
                        event,
                        provider=raw.provider,
                        signature=raw.signature,
                        slot=raw.slot,
                        commitment=Commitment(raw.commitment),
                        observed_at=raw.received_at,
                        raw_event_id=raw.id,
                    )
                    break
                except UnsupportedNormalizationError:
                    continue
            if normalized is None:
                raise ReplayError("replay_normalization_unavailable")
            canonical = CanonicalEventRepository(self._source).insert_if_absent(normalized)
            LifecycleRepository(self._source).apply(canonical)
            count += 1
        return count

    def _process(self, raw_id: int, report: ReplayReport) -> None:
        report.raw_read += 1
        report.last_raw_id = raw_id
        session, owned = RawJournal(self._source)._session()
        try:
            raw = RawJournal._record(
                session.execute(select(raw_events).where(raw_events.c.id == raw_id)).one()
            )
        finally:
            if owned:
                session.close()
        try:
            if hashlib.sha256(raw.payload_json.encode()).hexdigest() != raw.payload_sha256:
                raise ReplayError("replay_payload_hash_mismatch")
            if raw.safe_error_code == "failed_transaction":
                report.raw_only += 1
                report.warnings.append("failed_transaction")
                return
            decoder_input = DecoderInput(
                raw_event_id=raw.id, transport=raw.transport, payload_json=raw.payload_json
            )
            if raw.transport == "fixture":
                result = self._fixture.decode(decoder_input)
            elif (
                raw.transport in {"logs_subscribe", "parsed_streams"}
                and {"transaction", "meta", "slot"} <= json.loads(raw.payload_json).keys()
            ):
                result = RawLogsDecoder(self._settings, accepted_transport=raw.transport).decode(
                    decoder_input
                )
            else:
                report.raw_only += 1
                report.warnings.append("replay_context_unavailable")
                return
            if raw.status != "decoded":
                DecodeOutcomeRepository(self._source).record_outcome(
                    raw_event_id=raw.id,
                    status=result.status,
                    safe_error_code=result.safe_error_code,
                    safe_error_message=result.safe_error_message,
                )
            if result.status == DecodeStatus.DECODED:
                report.canonical_processed += self.apply_decoded(result)
                report.decoded += 1
            elif result.status == DecodeStatus.RAW_ONLY:
                report.raw_only += 1
                report.warnings.append(result.safe_error_code or "replay_raw_only")
            else:
                report.failed += 1
                report.warnings.append(result.safe_error_code or "replay_decode_failed")
        except Exception as error:
            report.failed += 1
            report.warnings.append(
                error.code if isinstance(error, ReplayError) else "replay_processing_failed"
            )

    def journal(self, *, after_id: int = 0, limit: int = 1000) -> ReplayReport:
        if (
            type(limit) is not int
            or not 1 <= limit <= 1000
            or type(after_id) is not int
            or after_id < 0
        ):
            raise ReplayError("replay_bounds_invalid")
        session, owned = RawJournal(self._source)._session()
        try:
            ids: list[int] = list(
                session.scalars(
                    select(raw_events.c.id)
                    .where(raw_events.c.id > after_id)
                    .order_by(raw_events.c.id)
                    .limit(limit + 1)
                )
            )
        finally:
            if owned:
                session.close()
        report = ReplayReport(truncated=len(ids) > limit)
        for raw_id in ids[:limit]:
            self._process(raw_id, report)
        report.warnings = sorted(set(report.warnings))
        return report

    def fixtures(
        self, paths: list[str | Path], *, passes: int = 1, limit: int = 1000
    ) -> ReplayReport:
        if (
            type(passes) is not int
            or not 1 <= passes <= 2
            or type(limit) is not int
            or not 1 <= limit <= 1000
        ):
            raise ReplayError("replay_bounds_invalid")
        fixtures = FixtureTransport(paths).load_all()
        if len(fixtures) > limit:
            raise ReplayError("replay_bounds_invalid")
        report = ReplayReport()
        for _ in range(passes):
            for item in fixtures:
                raw = RawJournal(self._source).insert(item.envelope)
                if item.envelope.transaction_error is not None:
                    DecodeOutcomeRepository(self._source).record_outcome(
                        raw_event_id=raw.id,
                        status=DecodeStatus.RAW_ONLY,
                        safe_error_code="failed_transaction",
                        safe_error_message="transaction execution failed",
                    )
                self._process(raw.id, report)
        report.warnings = sorted(set(report.warnings))
        return report
