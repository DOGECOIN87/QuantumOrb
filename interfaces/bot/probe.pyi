"""`qorb check`: ask every endpoint the bot depends on, from THIS machine, and print what came back."""
# QuantumOrb · interface contract · quantumorb/probe.py
# implementation: sealed · commitment 604ac6f549b19c06…2d13870c
# signatures only - the bodies are in the sealed source (see SEAL.md)
from __future__ import annotations

from typing import Any

async def probe(session: aiohttp.ClientSession, title: str, url: str, params: dict | None = ..., out = ..., method: str = ..., payload: Any = ...) -> Any | None: ...

async def check_telegram(out = ...) -> bool: ...

async def check_rpc(out = ..., mint: str | None = ...) -> None: ...

async def run(out = ...) -> None: ...
