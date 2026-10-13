"""Print validated settings without exposing credentials."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from app.config import ConfigurationError, IngestMode, load_settings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--demo", action="store_true", help="validate safe demo settings")
    mode.add_argument(
        "--live", action="store_true", help="validate live settings and require a key"
    )
    args = parser.parse_args(argv)

    override: IngestMode | None = "demo" if args.demo else "live" if args.live else None
    try:
        settings = load_settings(mode_override=override)
    except ConfigurationError as error:
        print(f"Configuration invalid: {error}", file=sys.stderr)
        return 2

    print(json.dumps({"status": "valid", "settings": settings.safe_diagnostics()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
