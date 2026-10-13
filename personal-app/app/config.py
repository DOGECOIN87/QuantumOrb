"""Minimal defaults for the T001 demo bootstrap.

Strict environment parsing and live-mode validation are intentionally deferred to T002.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    """Non-secret defaults that allow the app to start without provider credentials."""

    app_env: str = "development"
    ingest_mode: str = "demo"
