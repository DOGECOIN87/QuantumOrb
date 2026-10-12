# QuantumOrb · interface contract · quantumorb/config.py
# implementation: sealed · commitment 0c22bd76d733a5c0…7639a407
# signatures only - the bodies are in the sealed source (see SEAL.md)
from __future__ import annotations

from typing import Any

def proxy_url(value: str | None) -> str | None: ...

def mask(url: str | None) -> str:
    """A proxy or RPC URL as it may appear in a log: no login, no password, no api key."""
    ...

class Settings:
    def __init__(self) -> None: ...
    def reload(self) -> None: ...
    def secrets(self) -> list[str]:
        """Values that must never reach a log line (see cli.Redacting)."""
        ...
