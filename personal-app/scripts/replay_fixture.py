"""Replay local provider fixtures twice through the raw event journal, without network access."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.db.models import raw_events
from app.db.raw_journal import RawJournal, RawJournalError
from app.db.session import create_database_engine, create_session_factory, ensure_sqlite_parent
from app.integrations.fixture_transport import FixtureTransport, FixtureTransportError

_DEFAULT_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "coding-pack" / "fixtures" / "helius"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "fixtures",
        nargs="*",
        help="local fixture JSON files or directories (default: checked-in Helius fixtures)",
    )
    parser.add_argument(
        "--database-url",
        help="database URL (default: DATABASE_URL or the app's local SQLite setting)",
    )
    args = parser.parse_args(argv)

    try:
        settings = Settings(ingest_mode="demo", ingest_transport="fixture")
        sources = args.fixtures or [_DEFAULT_FIXTURE_DIR]
        fixtures = FixtureTransport(sources).load_all()
        database_url = args.database_url or settings.database_url
        ensure_sqlite_parent(database_url)
        engine = create_database_engine(database_url)
        try:
            journal = RawJournal(create_session_factory(engine))
            first_pass = [journal.insert(item.envelope) for item in fixtures]
            second_pass = [journal.insert(item.envelope) for item in fixtures]
            first_ids = [record.id for record in first_pass]
            second_ids = [record.id for record in second_pass]
            if first_ids != second_ids:
                raise RawJournalError("fixture replay produced different row identities")
            with engine.connect() as connection:
                total_rows = connection.execute(
                    select(func.count()).select_from(raw_events)
                ).scalar_one()
        finally:
            engine.dispose()
    except FixtureTransportError as error:
        print(f"Fixture replay failed: {error}", file=sys.stderr)
        return 2
    except RawJournalError as error:
        print(f"Fixture replay failed: {error}", file=sys.stderr)
        return 2
    except SQLAlchemyError:
        print(
            "Fixture replay failed: database operation failed; verify the database URL "
            "and run migrations.",
            file=sys.stderr,
        )
        return 2
    except ValidationError:
        print("Fixture replay failed: application configuration is invalid.", file=sys.stderr)
        return 2
    except ValueError as error:
        print(f"Fixture replay failed: {error}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "status": "ok",
                "fixture_count": len(fixtures),
                "replay_passes": 2,
                "stable_fixture_rows": len(set(first_ids)),
                "total_raw_event_rows": total_rows,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
