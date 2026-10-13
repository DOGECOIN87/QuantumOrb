"""Offline transport adapter for checked-in provider fixture envelopes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.domain.models import ProviderEnvelope


class FixtureTransportError(ValueError):
    """A local fixture could not be safely loaded as a provider envelope."""


@dataclass(frozen=True)
class FixtureEnvelope:
    """A validated provider envelope paired with its local source path."""

    path: Path
    envelope: ProviderEnvelope


class FixtureTransport:
    """Read local JSON fixtures through the same typed envelope used by providers."""

    def __init__(self, sources: list[str | Path] | tuple[str | Path, ...]) -> None:
        self._sources = tuple(Path(source) for source in sources)
        if not self._sources:
            raise FixtureTransportError("at least one local fixture path is required")

    def _paths(self) -> tuple[Path, ...]:
        paths: list[Path] = []
        for source in self._sources:
            if source.is_file():
                paths.append(source)
                continue
            if source.is_dir():
                files = sorted(path for path in source.glob("*.json") if path.is_file())
                if not files:
                    raise FixtureTransportError(f"no JSON fixtures found in {source.name}")
                paths.extend(files)
                continue
            raise FixtureTransportError(f"fixture path does not exist: {source.name}")
        if not paths:
            raise FixtureTransportError("no local JSON fixtures were selected")
        return tuple(paths)

    def load_all(self) -> tuple[FixtureEnvelope, ...]:
        """Load and validate every selected fixture before the caller mutates the database."""
        loaded: list[FixtureEnvelope] = []
        for path in self._paths():
            try:
                payload: Any = json.loads(path.read_text(encoding="utf-8"))
                envelope = ProviderEnvelope.model_validate(payload)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError):
                raise FixtureTransportError(
                    f"invalid provider envelope fixture: {path.name}"
                ) from None
            loaded.append(FixtureEnvelope(path=path, envelope=envelope))
        return tuple(loaded)
