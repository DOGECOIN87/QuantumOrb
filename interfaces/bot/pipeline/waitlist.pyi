"""The Signals waitlist: one announcement to everyone who pressed "Notify me", then the list is gone."""
# QuantumOrb · interface contract · quantumorb/pipeline/waitlist.py
# implementation: sealed · commitment 177728158fe885e6…5bd3329c
# signatures only - the bodies are in the sealed source (see SEAL.md)
from __future__ import annotations

from typing import Any

async def broadcast(bot, db, text: str, keep: bool = ..., gap_s: float = ...) -> dict: ...
