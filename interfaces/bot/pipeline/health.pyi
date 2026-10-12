"""What is quietly broken."""
# QuantumOrb · interface contract · quantumorb/pipeline/health.py
# implementation: sealed · commitment d2d7e849953c6654…6dc7e644
# signatures only - the bodies are in the sealed source (see SEAL.md)
from __future__ import annotations

from typing import Any

def expected_loops() -> dict[str, float]:
    """name -> the interval its heartbeat should arrive at."""
    ...

def checks(beats: dict[str, dict], now: int, live: dict | None = ...) -> list[dict]:
    """Heartbeats (+ what the running process knows) -> [{name, ok, detail}]."""
    ...

def all_ok(rows: list[dict]) -> bool: ...
