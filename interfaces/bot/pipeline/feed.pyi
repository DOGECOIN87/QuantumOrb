"""The new-launch feed: which of the latest launches a chat should hear about."""
# QuantumOrb · interface contract · quantumorb/pipeline/feed.py
# implementation: sealed · commitment 0b60dfd5734c1364…62ed31c4
# signatures only - the bodies are in the sealed source (see SEAL.md)
from __future__ import annotations

from typing import Any

def passes_filters(coin: dict, s: dict) -> bool: ...

def merge_window(pages: list[list[dict]]) -> list[dict]:
    """Pages of /coins -> one list, deduplicated by mint, oldest first (so alerts go out in launch order)."""
    ...

class Memory:
    """A set that forgets its oldest entries past `limit`: what was already checked or sent."""
    def __init__(self, limit: int): ...
    def add(self, key: Hashable) -> None: ...
    def clear(self) -> None: ...
