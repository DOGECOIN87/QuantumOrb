"""/watch: one token, the moments its subscriber asked to hear about."""
# QuantumOrb · interface contract · quantumorb/pipeline/watch.py
# implementation: sealed · commitment 262d7641a4b5dc70…6156e6cc
# signatures only - the bodies are in the sealed source (see SEAL.md)
from __future__ import annotations

from typing import Any

def milestone_floor(mcap: float | None) -> float:
    """The highest milestone MC has already passed (0 if none)."""
    ...

def check(coin: dict, w: dict) -> tuple[dict, list[tuple[str, float, float]]]:
    """One watch against a fresh coin -> (db updates, reasons)."""
    ...
