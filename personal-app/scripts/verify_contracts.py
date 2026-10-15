"""Check schemas, pinned IDLs, migrations, and Scout golden vectors without a provider."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from alembic.config import Config
from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]
from sqlalchemy import func, select

from alembic import command
from app.config import Settings
from app.db.catalog import load_pinned_catalog
from app.db.graph import utc
from app.db.models import raw_events, scout_events
from app.db.session import create_database_engine, create_session_factory
from app.domain.models import ScoutEventRecord
from app.integrations.fixture_transport import FixtureTransport
from app.replay import ReplayService

ROOT = Path(__file__).resolve().parents[2]
# These are intentionally narrow, checked-in stable contracts. Do not derive the
# selected fields from the expected JSON: a deleted expected field must fail.
VECTORS = (
    (
        "pump-create-event.json",
        "create-event.expected.json",
        (
            "event_type",
            "program",
            "event_name",
            "mint",
            "bonding_curve",
            "user",
            "creator",
            "quote_mint",
            "decode_status",
        ),
    ),
    (
        "pump-trade-event.json",
        "trade-event.expected.json",
        ("event_type", "program", "event_name", "mint", "user", "decode_status"),
    ),
    (
        "pumpswap-create-pool-event.json",
        "pool-event.expected.json",
        ("event_type", "program", "event_name", "pool", "base_mint", "quote_mint", "decode_status"),
    ),
)


def mismatch_paths(actual: Any, expected: Any, path: str = "$") -> list[str]:
    """Report every changed, missing, or extra field, without printing its value."""
    if type(actual) is not type(expected):
        return [path]
    if isinstance(actual, dict):
        mismatches = []
        for key in sorted(actual.keys() | expected.keys()):
            child = f"{path}.{key}"
            if key not in actual or key not in expected:
                mismatches.append(child)
            else:
                mismatches.extend(mismatch_paths(actual[key], expected[key], child))
        return mismatches
    if isinstance(actual, list):
        mismatches = []
        for index in range(max(len(actual), len(expected))):
            child = f"{path}[{index}]"
            if index >= len(actual) or index >= len(expected):
                mismatches.append(child)
            else:
                mismatches.extend(mismatch_paths(actual[index], expected[index], child))
        return mismatches
    return [] if actual == expected else [path]


def verify_contracts(*, golden_dir: Path | None = None) -> dict[str, Any]:
    schemas = {}
    for directory in (ROOT / "schemas", ROOT / "coding-pack/schemas"):
        for path in sorted(directory.glob("*.schema.json")):
            schema = json.loads(path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
            schemas[path.name] = Draft202012Validator(schema, format_checker=FormatChecker())
    if len(schemas) < 8:
        raise ValueError("required schema files are missing")
    options: dict[str, Any] = {
        "_env_file": None,
        "ingest_mode": "demo",
        "ingest_transport": "fixture",
    }
    settings = Settings(**options)
    catalog = load_pinned_catalog(settings)
    goldens = golden_dir if golden_dir is not None else ROOT / "coding-pack/golden-tests"
    mismatches: list[str] = []
    with TemporaryDirectory(prefix="quantumorb-contracts-") as temporary_directory:
        database_url = f"sqlite:///{Path(temporary_directory) / 'contracts.db'}"
        config = Config(stdout=StringIO())
        config.set_main_option("script_location", str(ROOT / "personal-app/alembic"))
        config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
        command.upgrade(config, "head")
        command.check(config)
        engine = create_database_engine(database_url)
        try:
            factory = create_session_factory(engine)
            replay = ReplayService(factory, settings)
            for fixture_name, golden_name, fields in VECTORS:
                fixture = ROOT / "coding-pack/fixtures/helius" / fixture_name
                envelope = FixtureTransport([fixture]).load_all()[0].envelope
                schemas["provider-envelope.schema.json"].validate(
                    envelope.model_dump(mode="json", by_alias=True, exclude_none=True)
                )
                report = replay.fixtures([fixture], passes=2)
                if report.failed or report.canonical_processed != 2:
                    mismatches.append(f"{fixture_name}: $.replay")
                    continue
                with factory() as session:
                    row = dict(
                        session.execute(
                            select(scout_events).where(
                                scout_events.c.signature == envelope.signature
                            )
                        )
                        .one()
                        ._mapping
                    )
                row["payload"] = json.loads(row["payload_json"])
                if row["block_time"] is not None:
                    row["block_time"] = utc(row["block_time"])
                record = ScoutEventRecord.model_validate(
                    {field: row[field] for field in ScoutEventRecord.model_fields}
                )
                schemas["scout-event.schema.json"].validate(record.model_dump(mode="json"))
                # The pool vector names the normalized base mint explicitly.
                actual = {field: row["mint" if field == "base_mint" else field] for field in fields}
                expected = json.loads((goldens / golden_name).read_text(encoding="utf-8"))
                mismatches.extend(
                    f"{fixture_name}: {path}" for path in mismatch_paths(actual, expected)
                )
            with factory() as session:
                for table in (raw_events, scout_events):
                    if session.scalar(select(func.count()).select_from(table)) != len(VECTORS):
                        mismatches.append(f"fixture replay: $.{table.name}.count")
        finally:
            engine.dispose()
    return {
        "status": "failed" if mismatches else "ok",
        "schemas_checked": len(schemas),
        "idl_snapshots_checked": len(catalog),
        "golden_vectors_checked": len(VECTORS),
        "replay_passes": 2,
        "migration_consistent": True,
        "mismatches": mismatches,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--golden-dir", type=Path, help="override the Scout golden vector directory"
    )
    args = parser.parse_args(argv)
    try:
        report = verify_contracts(golden_dir=args.golden_dir)
    except Exception:
        print(
            "Contract verification failed; check local schemas, IDLs, fixtures and migrations.",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
