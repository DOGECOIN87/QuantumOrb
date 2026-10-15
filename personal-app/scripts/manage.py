"""Local operational commands. Provider work requires an explicit --live flag."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from alembic.config import Config

from alembic import command
from app.cartographer import CartographerService
from app.config import Settings
from app.db.session import create_database_engine, create_session_factory, ensure_sqlite_parent
from app.decoders import DecodeResult
from app.integrations.stream_supervisor import StreamSupervisor
from app.operations import Operations
from app.replay import ReplayService


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db")
    commands.add_parser("health")
    commands.add_parser("metrics")
    replay = commands.add_parser("replay")
    replay.add_argument("fixtures", nargs="*")
    replay.add_argument("--passes", type=int, choices=[1, 2], default=1)
    replay.add_argument("--after-id", type=int, default=0)
    replay.add_argument("--limit", type=int, default=1000)
    export = commands.add_parser("export-graph")
    export.add_argument("--mint")
    export.add_argument("--limit", type=int, default=1000)
    export.add_argument("--output", type=Path)
    analyze = commands.add_parser("analyze-token")
    analyze.add_argument("mint")
    analyze.add_argument("--live", action="store_true")
    listen = commands.add_parser("listen")
    listen.add_argument("--live", action="store_true", required=True)
    args = parser.parse_args(argv)
    try:
        live = args.command == "listen" or (args.command == "analyze-token" and args.live)
        settings = Settings() if live else Settings(ingest_mode="demo", ingest_transport="fixture")
        if live and settings.ingest_mode != "live":
            raise ValueError("live configuration is required")
        database_url = args.database_url or settings.database_url
        if not database_url.startswith("sqlite:///"):
            raise ValueError("local SQLite storage is required")
        if args.command == "init-db":
            ensure_sqlite_parent(database_url)
            config = Config()
            config.set_main_option(
                "script_location", str(Path(__file__).resolve().parents[1] / "alembic")
            )
            config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
            command.upgrade(config, "head")
            print(json.dumps({"status": "initialized"}))
            return 0
        engine = create_database_engine(database_url)
        try:
            factory = create_session_factory(engine)
            operations = Operations(factory, settings)
            if args.command == "health":
                result = operations.health()
                print(json.dumps(result, sort_keys=True))
                return 0 if result["ready"] else 2
            if args.command == "metrics":
                print(operations.metrics(), end="")
                return 0
            if not operations.health()["ready"]:
                raise ValueError("database is not ready")
            if args.command == "export-graph":
                exported = (
                    json.dumps(
                        operations.export_graph(mint=args.mint, limit=args.limit),
                        sort_keys=True,
                        indent=2,
                    )
                    + "\n"
                )
                if args.output:
                    with args.output.open("x", encoding="utf-8") as destination:
                        destination.write(exported)
                    print(json.dumps({"status": "exported", "redacted": True}))
                else:
                    print(exported, end="")
            elif args.command == "replay":
                if (args.fixtures and args.after_id != 0) or (
                    not args.fixtures and args.passes != 1
                ):
                    raise ValueError("replay options do not match the selected source")
                replayer = ReplayService(factory, settings)
                report = (
                    replayer.fixtures(args.fixtures, passes=args.passes, limit=args.limit)
                    if args.fixtures
                    else replayer.journal(after_id=args.after_id, limit=args.limit)
                )
                print(json.dumps(asdict(report), sort_keys=True))
                return 2 if report.failed else 0
            elif args.command == "analyze-token":
                service = CartographerService(factory, settings)
                result_analysis = (
                    asyncio.run(service.backfill(args.mint, observed_at=datetime.now(UTC)))
                    if args.live
                    else service.analyze(args.mint, observed_at=datetime.now(UTC))
                )
                print(result_analysis.summary.model_dump_json(indent=2))
            elif args.command == "listen":
                replayer = ReplayService(factory, settings)

                async def consume(result: DecodeResult) -> None:
                    replayer.apply_decoded(result)

                asyncio.run(StreamSupervisor(settings, factory).run(consume))
            return 0
        finally:
            engine.dispose()
    except KeyboardInterrupt:
        print('{"status":"stopped"}')
        return 130
    except Exception:
        print(
            "Operation failed; check local configuration, migrations, and bounded inputs.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
