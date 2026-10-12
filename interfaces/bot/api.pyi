"""`qorb serve`: a small read-only HTTP API over the bot's record and its Guard."""
# QuantumOrb · interface contract · quantumorb/api.py
# implementation: sealed · commitment 6d2d11c247196b24…202f1452
# signatures only - the bodies are in the sealed source (see SEAL.md)
from __future__ import annotations

from typing import Any

def token_view(coin: dict, creator: dict | None, chain: dict | None) -> dict: ...

def build_app(db: DB, api = ..., rpc = ...) -> web.Application: ...

async def serve() -> None: ...
