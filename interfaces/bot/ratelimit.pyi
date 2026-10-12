"""One gap between requests to the same host, shared by every loop in the process."""
# QuantumOrb · interface contract · quantumorb/ratelimit.py
# implementation: sealed · commitment cff4721d46cbecb2…ac4fbfcc
# signatures only - the bodies are in the sealed source (see SEAL.md)
from __future__ import annotations

from typing import Any

class Throttle:
    def __init__(self, min_interval: float): ...
    async def wait(self) -> None: ...

class Window:
    """At most `limit` events per `seconds`, per key."""
    def __init__(self, limit: int, seconds: float): ...
    def allow(self, key: object, now: float | None = ...) -> bool: ...
    def clear(self) -> None: ...
