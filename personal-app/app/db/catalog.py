"""Offline-first loading and immutable persistence for provider IDL snapshots."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import provider_catalog_snapshots

Program = Literal["pump", "pump_amm"]
SessionSource = Session | sessionmaker[Session] | Callable[[], Session]


class CatalogError(ValueError):
    """Base class for safe provider catalog boundary errors."""


class CatalogAddressError(CatalogError):
    """The expected or declared program address is invalid or mismatched."""


class CatalogPayloadError(CatalogError):
    """The IDL payload is not a valid JSON object with required identity fields."""


class CatalogSnapshotConflictError(CatalogError):
    """A pinned program revision already exists with different immutable data."""


class CatalogSnapshotNotFoundError(CatalogError):
    """An explicit activation request referenced a snapshot that does not exist."""


@dataclass(frozen=True)
class CatalogSnapshotInput:
    """Validated catalog data ready for insert; new snapshots are always inactive."""

    program_id: str
    program_name: str
    idl_revision: str
    payload_json: str
    content_sha256: str
    observed_at: datetime
    source_url: str


@dataclass(frozen=True)
class CatalogSnapshotRecord:
    """A persisted provider catalog snapshot."""

    id: int
    program_id: str
    program_name: str | None
    idl_revision: str
    payload_json: str
    content_sha256: str
    observed_at: datetime
    source_url: str
    active: bool


def _validate_program_address(value: object, *, field: str) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    if not isinstance(value, str) or not 32 <= len(value) <= 44:
        raise CatalogAddressError(f"{field} must be a valid 32-byte Base58 program address")

    decoded_number = 0
    for character in value:
        if character not in alphabet:
            raise CatalogAddressError(f"{field} must be a valid 32-byte Base58 program address")
        decoded_number = decoded_number * 58 + alphabet.index(character)

    decoded_bytes = (decoded_number.bit_length() + 7) // 8
    leading_zero_bytes = len(value) - len(value.lstrip("1"))
    if decoded_bytes + leading_zero_bytes != 32:
        raise CatalogAddressError(f"{field} must be a valid 32-byte Base58 program address")
    return value


def _is_json_value(value: object) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())
    return False


def canonical_catalog_json(payload: object) -> str:
    """Return compact, sorted JSON for an object containing only JSON values."""
    if not isinstance(payload, dict) or not _is_json_value(payload):
        raise CatalogPayloadError("catalog payload must be a JSON object")
    try:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise CatalogPayloadError("catalog payload must be a JSON object") from None


def _required_text(value: object, *, field: str, max_length: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > max_length:
        raise CatalogPayloadError(f"{field} must be non-empty text within the supported length")
    return value


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise CatalogPayloadError("observed_at must be timezone-aware")
    return value.astimezone(UTC)


def _expected_program_id(program: Program, settings: Settings) -> str:
    if program == "pump":
        return settings.pump_program_id
    return settings.pump_amm_program_id


def validate_catalog_payload(
    payload: object,
    *,
    expected_program_id: str,
    program_name: str,
    idl_revision: str,
    source_url: str,
    observed_at: datetime | None = None,
    expected_sha256: str | None = None,
) -> CatalogSnapshotInput:
    """Validate address, revision, provenance, and hash without accessing the network."""
    expected = _validate_program_address(expected_program_id, field="expected program address")
    if not isinstance(payload, dict):
        raise CatalogPayloadError("catalog payload must be a JSON object")
    serialized = canonical_catalog_json(payload)
    declared_address = _validate_program_address(
        payload.get("address"), field="declared IDL address"
    )
    if declared_address != expected:
        raise CatalogAddressError("declared IDL address does not match expected program address")

    revision = _required_text(idl_revision, field="idl_revision", max_length=128)
    name = _required_text(program_name, field="program_name", max_length=64)
    provenance = _required_text(source_url, field="source_url", max_length=4096)
    content_sha256 = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    if expected_sha256 is not None:
        if (
            len(expected_sha256) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha256)
            or expected_sha256 != content_sha256
        ):
            raise CatalogPayloadError("supplied content SHA-256 does not match canonical payload")

    return CatalogSnapshotInput(
        program_id=expected,
        program_name=name,
        idl_revision=revision,
        payload_json=serialized,
        content_sha256=content_sha256,
        observed_at=_utc(observed_at),
        source_url=provenance,
    )


def load_local_idl(
    path: str | Path,
    *,
    program: Program,
    settings: Settings,
    idl_revision: str,
    source_url: str | None = None,
    observed_at: datetime | None = None,
    expected_sha256: str | None = None,
) -> CatalogSnapshotInput:
    """Load a JSON IDL only from the supplied local path; this function never downloads."""
    local_path = Path(path)
    try:
        payload = json.loads(local_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise CatalogPayloadError("could not read a valid local IDL JSON file") from None

    provenance = source_url if source_url is not None else local_path.resolve().as_uri()
    return validate_catalog_payload(
        payload,
        expected_program_id=_expected_program_id(program, settings),
        program_name=program,
        idl_revision=idl_revision,
        source_url=provenance,
        observed_at=observed_at,
        expected_sha256=expected_sha256,
    )


_PINNED_MANIFEST = Path(__file__).resolve().parents[2] / "catalog" / "idl" / "manifest.json"
_PINNED_REPOSITORY = "https://github.com/pump-fun/pump-public-docs"
_PINNED_PROGRAM_FILES: tuple[tuple[Program, str], ...] = (
    ("pump", "pump.json"),
    ("pump_amm", "pump_amm.json"),
)


def load_pinned_catalog(
    settings: Settings,
    *,
    manifest_path: str | Path | None = None,
    observed_at: datetime | None = None,
) -> tuple[CatalogSnapshotInput, ...]:
    """Load checked-in Pump IDLs and verify source pin, hashes, and configured addresses."""
    path = _PINNED_MANIFEST if manifest_path is None else Path(manifest_path)
    try:
        manifest: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise CatalogPayloadError("could not read the pinned catalog manifest") from None

    if (
        not isinstance(manifest, dict)
        or type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != 1
        or manifest.get("source_repository") != _PINNED_REPOSITORY
    ):
        raise CatalogPayloadError("pinned catalog manifest metadata is invalid")

    commit = manifest.get("source_commit")
    verified_at = manifest.get("verified_at")
    if (
        not isinstance(commit, str)
        or len(commit) != 40
        or any(character not in "0123456789abcdef" for character in commit)
        or not isinstance(verified_at, str)
    ):
        raise CatalogPayloadError("pinned catalog manifest revision is invalid")
    try:
        if date.fromisoformat(verified_at).isoformat() != verified_at:
            raise ValueError
    except ValueError:
        raise CatalogPayloadError("pinned catalog manifest verification date is invalid") from None

    programs = manifest.get("programs")
    if not isinstance(programs, dict) or set(programs) != {
        program for program, _ in _PINNED_PROGRAM_FILES
    }:
        raise CatalogPayloadError("pinned catalog manifest program list is invalid")

    snapshot_time = _utc(observed_at)
    snapshots: list[CatalogSnapshotInput] = []
    for program, filename in _PINNED_PROGRAM_FILES:
        entry = programs[program]
        expected_url = f"{_PINNED_REPOSITORY}/blob/{commit}/idl/{filename}"
        if (
            not isinstance(entry, dict)
            or entry.get("file") != filename
            or entry.get("source_url") != expected_url
            or not isinstance(entry.get("canonical_sha256"), str)
        ):
            raise CatalogPayloadError("pinned catalog manifest entry is invalid")
        snapshots.append(
            load_local_idl(
                path.parent / filename,
                program=program,
                settings=settings,
                idl_revision=commit,
                source_url=expected_url,
                observed_at=snapshot_time,
                expected_sha256=entry["canonical_sha256"],
            )
        )
    return tuple(snapshots)


def _record(row: Any) -> CatalogSnapshotRecord:
    values = dict(row._mapping if hasattr(row, "_mapping") else row)
    observed_at = values["observed_at"]
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        values["observed_at"] = observed_at.replace(tzinfo=UTC)
    else:
        values["observed_at"] = observed_at.astimezone(UTC)
    return CatalogSnapshotRecord(**values)


class CatalogSnapshotRepository:
    """Insert-only snapshot repository with explicit, separate active selection."""

    def __init__(self, session_source: SessionSource) -> None:
        self._session_source = session_source

    def _session(self) -> tuple[Session, bool]:
        if isinstance(self._session_source, Session):
            return self._session_source, False
        return self._session_source(), True

    @staticmethod
    def _same_snapshot(existing: Any, snapshot: CatalogSnapshotInput) -> bool:
        stored = existing._mapping if hasattr(existing, "_mapping") else existing
        return bool(
            stored["payload_json"] == snapshot.payload_json
            and stored["content_sha256"] == snapshot.content_sha256
            and stored["program_name"] == snapshot.program_name
            and stored["source_url"] == snapshot.source_url
        )

    def insert_if_absent(self, snapshot: CatalogSnapshotInput) -> CatalogSnapshotRecord:
        """Insert a revision once; identical retries return the original row unchanged."""
        session, owns_session = self._session()
        identity = (snapshot.program_id, snapshot.idl_revision)
        try:
            existing = session.execute(
                select(provider_catalog_snapshots).where(
                    provider_catalog_snapshots.c.program_id == identity[0],
                    provider_catalog_snapshots.c.idl_revision == identity[1],
                )
            ).first()
            if existing is not None:
                if not self._same_snapshot(existing, snapshot):
                    raise CatalogSnapshotConflictError(
                        "program revision already exists with different immutable data"
                    )
                session.commit()
                return _record(existing)

            values = {
                "program_id": snapshot.program_id,
                "program_name": snapshot.program_name,
                "idl_revision": snapshot.idl_revision,
                "payload_json": snapshot.payload_json,
                "content_sha256": snapshot.content_sha256,
                "observed_at": snapshot.observed_at,
                "source_url": snapshot.source_url,
                "active": False,
            }
            try:
                session.execute(insert(provider_catalog_snapshots).values(values))
                session.commit()
            except IntegrityError:
                session.rollback()
                raced = session.execute(
                    select(provider_catalog_snapshots).where(
                        provider_catalog_snapshots.c.program_id == identity[0],
                        provider_catalog_snapshots.c.idl_revision == identity[1],
                    )
                ).first()
                if raced is None:
                    raise
                if not self._same_snapshot(raced, snapshot):
                    raise CatalogSnapshotConflictError(
                        "program revision already exists with different immutable data"
                    ) from None
                session.commit()
                return _record(raced)

            persisted = session.execute(
                select(provider_catalog_snapshots).where(
                    provider_catalog_snapshots.c.program_id == identity[0],
                    provider_catalog_snapshots.c.idl_revision == identity[1],
                )
            ).first()
            if persisted is None:
                raise CatalogError("catalog snapshot insert did not produce a persisted row")
            return _record(persisted)
        except Exception:
            if session.in_transaction():
                session.rollback()
            raise
        finally:
            if owns_session:
                session.close()

    def activate(self, *, program_id: str, idl_revision: str) -> CatalogSnapshotRecord:
        """Explicitly activate one pinned revision, deactivating peers for that program."""
        address = _validate_program_address(program_id, field="program address")
        revision = _required_text(idl_revision, field="idl_revision", max_length=128)
        session, owns_session = self._session()
        try:
            target = session.execute(
                select(provider_catalog_snapshots).where(
                    provider_catalog_snapshots.c.program_id == address,
                    provider_catalog_snapshots.c.idl_revision == revision,
                )
            ).first()
            if target is None:
                raise CatalogSnapshotNotFoundError("catalog snapshot revision does not exist")

            session.execute(
                update(provider_catalog_snapshots)
                .where(provider_catalog_snapshots.c.program_id == address)
                .values(active=False)
            )
            session.execute(
                update(provider_catalog_snapshots)
                .where(
                    provider_catalog_snapshots.c.program_id == address,
                    provider_catalog_snapshots.c.idl_revision == revision,
                )
                .values(active=True)
            )
            session.commit()
            selected = session.execute(
                select(provider_catalog_snapshots).where(
                    provider_catalog_snapshots.c.program_id == address,
                    provider_catalog_snapshots.c.idl_revision == revision,
                )
            ).first()
            if selected is None:
                raise CatalogError("catalog activation did not produce a persisted snapshot")
            return _record(selected)
        except Exception:
            if session.in_transaction():
                session.rollback()
            raise
        finally:
            if owns_session:
                session.close()
