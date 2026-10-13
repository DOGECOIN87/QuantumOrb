"""Offline acceptance tests for the pinned provider IDL catalog."""

from __future__ import annotations

import hashlib
import json
import socket
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.base import Base
from app.db.catalog import (
    CatalogAddressError,
    CatalogPayloadError,
    CatalogSnapshotConflictError,
    CatalogSnapshotNotFoundError,
    CatalogSnapshotRepository,
    load_local_idl,
    load_pinned_catalog,
    validate_catalog_payload,
)
from app.db.models import provider_catalog_snapshots

PROGRAM_ID = "11111111111111111111111111111111"
OTHER_PROGRAM_ID = "SysvarRent111111111111111111111111111111111"


def settings() -> Settings:
    return Settings(pump_program_id=PROGRAM_ID, pump_amm_program_id=OTHER_PROGRAM_ID)


def idl_payload(address: str = PROGRAM_ID, *, event_name: str = "CreateEvent") -> dict[str, object]:
    return {
        "address": address,
        "metadata": {"name": "pump", "version": "1.0.0"},
        "instructions": [{"name": "create", "discriminator": [24, 30, 200]}],
        "events": [{"name": event_name, "discriminator": [27, 114, 169]}],
    }


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session


def write_idl(path: Path, payload: dict[str, object] | None = None) -> Path:
    path.write_text(json.dumps(payload or idl_payload()), encoding="utf-8")
    return path


def load(path: Path, *, revision: str = "pump-commit-a", payload_hash: str | None = None):
    return load_local_idl(
        path,
        program="pump",
        settings=settings(),
        idl_revision=revision,
        expected_sha256=payload_hash,
        observed_at=datetime(2026, 10, 13, 8, 0, tzinfo=UTC),
    )


def test_valid_local_idl_is_canonicalized_and_inserted(db: Session, tmp_path: Path) -> None:
    source = write_idl(tmp_path / "pump.json")
    snapshot = load(source)

    stored = CatalogSnapshotRepository(db).insert_if_absent(snapshot)

    assert stored.program_id == PROGRAM_ID
    assert stored.program_name == "pump"
    assert stored.idl_revision == "pump-commit-a"
    assert stored.source_url == source.resolve().as_uri()
    assert stored.active is False
    assert json.loads(stored.payload_json) == idl_payload()
    assert db.scalar(select(func.count()).select_from(provider_catalog_snapshots)) == 1


def test_hash_is_lowercase_sha256_of_exact_stored_canonical_json(
    db: Session, tmp_path: Path
) -> None:
    snapshot = load(write_idl(tmp_path / "pump.json"))
    stored = CatalogSnapshotRepository(db).insert_if_absent(snapshot)

    assert stored.payload_json == json.dumps(idl_payload(), sort_keys=True, separators=(",", ":"))
    assert stored.content_sha256 == hashlib.sha256(stored.payload_json.encode("utf-8")).hexdigest()
    assert stored.content_sha256 == stored.content_sha256.lower()
    assert len(stored.content_sha256) == 64


def test_declared_program_address_must_match_configured_address(tmp_path: Path) -> None:
    source = write_idl(tmp_path / "wrong.json", idl_payload(OTHER_PROGRAM_ID))
    with pytest.raises(CatalogAddressError, match="does not match"):
        load(source)


def test_address_mismatch_fails_before_database_mutation(db: Session, tmp_path: Path) -> None:
    source = write_idl(tmp_path / "wrong.json", idl_payload(OTHER_PROGRAM_ID))
    with pytest.raises(CatalogAddressError):
        CatalogSnapshotRepository(db).insert_if_absent(load(source))
    assert db.scalar(select(func.count()).select_from(provider_catalog_snapshots)) == 0


@pytest.mark.parametrize("address", ["", "not-base58!", "1" * 31, "1" * 33])
def test_invalid_expected_or_declared_program_address_is_rejected(
    address: str, tmp_path: Path
) -> None:
    with pytest.raises(CatalogAddressError):
        validate_catalog_payload(
            idl_payload(),
            expected_program_id=address,
            program_name="pump",
            idl_revision="pump-commit-a",
            source_url="file:///tmp/pump.json",
        )

    source = write_idl(tmp_path / "bad-address.json", idl_payload(address))
    with pytest.raises(CatalogAddressError):
        load(source)


def test_empty_revision_and_missing_identity_metadata_are_rejected() -> None:
    with pytest.raises(CatalogPayloadError, match="idl_revision"):
        validate_catalog_payload(
            idl_payload(),
            expected_program_id=PROGRAM_ID,
            program_name="pump",
            idl_revision="",
            source_url="file:///tmp/pump.json",
        )

    missing_address = {"metadata": {"name": "pump"}, "instructions": []}
    with pytest.raises(CatalogAddressError, match="declared IDL address"):
        validate_catalog_payload(
            missing_address,
            expected_program_id=PROGRAM_ID,
            program_name="pump",
            idl_revision="pump-commit-a",
            source_url="file:///tmp/pump.json",
        )


def test_non_object_payload_is_rejected() -> None:
    with pytest.raises(CatalogPayloadError, match="JSON object"):
        validate_catalog_payload(
            ["not", "an", "IDL object"],
            expected_program_id=PROGRAM_ID,
            program_name="pump",
            idl_revision="pump-commit-a",
            source_url="file:///tmp/pump.json",
        )


def test_supplied_hash_must_match_canonical_payload() -> None:
    with pytest.raises(CatalogPayloadError, match="SHA-256"):
        validate_catalog_payload(
            idl_payload(),
            expected_program_id=PROGRAM_ID,
            program_name="pump",
            idl_revision="pump-commit-a",
            source_url="file:///tmp/pump.json",
            expected_sha256="0" * 64,
        )


def test_duplicate_revision_returns_original_without_overwrite(db: Session, tmp_path: Path) -> None:
    repository = CatalogSnapshotRepository(db)
    first = repository.insert_if_absent(load(write_idl(tmp_path / "first.json")))
    duplicate = repository.insert_if_absent(load(write_idl(tmp_path / "first.json")))

    assert duplicate.id == first.id
    assert duplicate.payload_json == first.payload_json
    assert duplicate.content_sha256 == first.content_sha256
    assert duplicate.source_url == first.source_url
    assert db.scalar(select(func.count()).select_from(provider_catalog_snapshots)) == 1


def test_conflicting_payload_for_same_revision_fails_closed(db: Session, tmp_path: Path) -> None:
    repository = CatalogSnapshotRepository(db)
    first = repository.insert_if_absent(load(write_idl(tmp_path / "first.json")))
    changed = load(write_idl(tmp_path / "changed.json", idl_payload(event_name="ChangedEvent")))

    with pytest.raises(CatalogSnapshotConflictError):
        repository.insert_if_absent(changed)
    persisted = (
        db.execute(
            select(provider_catalog_snapshots).where(provider_catalog_snapshots.c.id == first.id)
        )
        .one()
        ._mapping
    )
    assert persisted["payload_json"] == first.payload_json
    assert persisted["content_sha256"] == first.content_sha256
    assert db.scalar(select(func.count()).select_from(provider_catalog_snapshots)) == 1


def test_different_revisions_create_separate_inactive_snapshots(
    db: Session, tmp_path: Path
) -> None:
    repository = CatalogSnapshotRepository(db)
    first = repository.insert_if_absent(load(write_idl(tmp_path / "first.json")))
    second = repository.insert_if_absent(
        load(write_idl(tmp_path / "second.json"), revision="pump-commit-b")
    )

    assert first.id != second.id
    assert first.active is False
    assert second.active is False
    assert db.scalar(select(func.count()).select_from(provider_catalog_snapshots)) == 2


def test_observed_at_is_utc_aware_after_sqlite_retrieval(db: Session, tmp_path: Path) -> None:
    non_utc = datetime(2026, 10, 13, 3, 0, tzinfo=timezone(timedelta(hours=-5)))
    snapshot = load_local_idl(
        write_idl(tmp_path / "pump.json"),
        program="pump",
        settings=settings(),
        idl_revision="pump-commit-a",
        observed_at=non_utc,
    )

    stored = CatalogSnapshotRepository(db).insert_if_absent(snapshot)

    assert stored.observed_at == datetime(2026, 10, 13, 8, 0, tzinfo=UTC)
    assert stored.observed_at.utcoffset() == timedelta(0)


def test_revision_activation_is_explicit_and_new_insert_does_not_switch_active(
    db: Session, tmp_path: Path
) -> None:
    repository = CatalogSnapshotRepository(db)
    first = repository.insert_if_absent(load(write_idl(tmp_path / "first.json")))
    active_first = repository.activate(program_id=PROGRAM_ID, idl_revision=first.idl_revision)
    assert active_first.active is True

    second = repository.insert_if_absent(
        load(write_idl(tmp_path / "second.json"), revision="pump-commit-b")
    )
    current_rows = db.execute(
        select(provider_catalog_snapshots).order_by(provider_catalog_snapshots.c.id)
    ).all()
    assert [row._mapping["active"] for row in current_rows] == [True, False]

    active_second = repository.activate(program_id=PROGRAM_ID, idl_revision=second.idl_revision)
    current_rows = db.execute(
        select(provider_catalog_snapshots).order_by(provider_catalog_snapshots.c.id)
    ).all()
    assert active_second.active is True
    assert [row._mapping["active"] for row in current_rows] == [False, True]


def test_activation_of_unknown_revision_fails_without_changing_selection(
    db: Session, tmp_path: Path
) -> None:
    repository = CatalogSnapshotRepository(db)
    first = repository.insert_if_absent(load(write_idl(tmp_path / "first.json")))
    repository.activate(program_id=PROGRAM_ID, idl_revision=first.idl_revision)

    with pytest.raises(CatalogSnapshotNotFoundError):
        repository.activate(program_id=PROGRAM_ID, idl_revision="unknown-revision")
    persisted = db.execute(select(provider_catalog_snapshots)).one()._mapping
    assert persisted["active"] is True


def test_loader_uses_only_local_file_and_preserves_event_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access is forbidden in the local catalog loader")

    monkeypatch.setattr(socket, "create_connection", no_network)
    source = write_idl(tmp_path / "pump.json", idl_payload(event_name="OpaqueProviderEvent"))

    snapshot = load(source)

    assert json.loads(snapshot.payload_json)["events"][0]["name"] == "OpaqueProviderEvent"
    assert "file://" in snapshot.source_url


def test_checked_in_idls_load_offline_and_persist_as_inactive_snapshots(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("pinned catalog loading must not access the network")

    monkeypatch.setattr(socket, "create_connection", no_network)
    configured = Settings(
        ingest_mode="demo",
        ingest_transport="fixture",
        pump_program_id="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
        pump_amm_program_id="pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
    )
    observed_at = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)

    snapshots = load_pinned_catalog(configured, observed_at=observed_at)
    repository = CatalogSnapshotRepository(db)
    stored = [repository.insert_if_absent(snapshot) for snapshot in snapshots]

    assert [snapshot.program_name for snapshot in snapshots] == ["pump", "pump_amm"]
    assert {snapshot.idl_revision for snapshot in snapshots} == {
        "2293f9a66c654e9fe82dc5e8f4618538f24bb35f"
    }
    assert all(snapshot.observed_at == observed_at for snapshot in snapshots)
    assert all(record.active is False for record in stored)
    assert all(record.source_url.startswith("https://github.com/pump-fun/") for record in stored)
    assert all(
        record.content_sha256 == hashlib.sha256(record.payload_json.encode("utf-8")).hexdigest()
        for record in stored
    )
    assert db.scalar(select(func.count()).select_from(provider_catalog_snapshots)) == 2
