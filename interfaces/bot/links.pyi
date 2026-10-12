"""Every outside link the bot prints, in one place."""
# QuantumOrb · interface contract · quantumorb/links.py
# implementation: sealed · commitment 372c2d918f0c9fc8…7583fe28
# signatures only - the bodies are in the sealed source (see SEAL.md)
from __future__ import annotations

from typing import Any

def pumpfun_coin(mint: str) -> str: ...

def solscan_token(mint: str) -> str: ...

def solscan_account(address: str) -> str: ...

def dexscreener(mint: str) -> str: ...

def token_links(mint: str) -> dict[str, str]:
    """What the HTTP API returns beside a token: where to read it."""
    ...

def site() -> str | None: ...
