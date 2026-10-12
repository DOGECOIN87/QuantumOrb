"""/follow: a wallet's new positions and full exits, and the moment several of them agree."""
# QuantumOrb · interface contract · quantumorb/pipeline/follow.py
# implementation: sealed · commitment fbb3a84f96a4df79…e85941e5
# signatures only - the bodies are in the sealed source (see SEAL.md)
from __future__ import annotations

from typing import Any

def snapshot(tokens: list[dict]) -> dict[str, dict]: ...

def diff(prev: dict[str, dict], current: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """(opened, fully exited), in a stable order."""
    ...

def hot_groups(entries: list[tuple[str, int]], min_wallets: int, window_s: float) -> list[str] | None: ...
