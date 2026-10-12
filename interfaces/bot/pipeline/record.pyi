"""The record: every alert the bot sent, and what the token did in the hour after."""
# QuantumOrb · interface contract · quantumorb/pipeline/record.py
# implementation: sealed · commitment 6f6b53ef547c71aa…c3bf0053
# signatures only - the bodies are in the sealed source (see SEAL.md)
from __future__ import annotations

from typing import Any

def read_hour(alert: dict, coin: dict | None, window_s: float) -> dict | None:
    """One due alert + pump.fun's coin now -> the read, or None to try again later."""
    ...

def stats(alerts: list[dict], stake: float = ...) -> dict: ...

def by_kind(alerts: list[dict], stake: float = ...) -> dict[str, dict]: ...

async def read_due(db, api, window_s: float, now: int, limit: int = ...) -> int:
    """Read every alert older than the window that has not been read."""
    ...
