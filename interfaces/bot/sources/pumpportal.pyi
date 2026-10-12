"""PumpPortal's websocket: new launches and migrations the moment they land, instead of a poll later."""
# QuantumOrb · interface contract · quantumorb/sources/pumpportal.py
# implementation: sealed · commitment 39603507ff2c0bca…490a254d
# signatures only - the bodies are in the sealed source (see SEAL.md)
from __future__ import annotations

from typing import Any

def parse_event(raw: str | bytes | dict) -> dict | None:
    """One websocket message -> {"kind": "create"|"migrate", "mint", "creator", "signature"} or None."""
    ...

class PumpPortalStream:
    """One socket for the life of the process; on_event is awaited for every parsed event."""
    def __init__(self, url: str, on_event: Handler, proxy: str | None = ..., min_backoff: float = ..., max_backoff: float = ...): ...
    async def run(self) -> None: ...
