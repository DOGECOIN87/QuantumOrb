"""Guard: the checks a launch has to pass before anybody is told about it."""
# QuantumOrb · interface contract · quantumorb/pipeline/guard.py
# implementation: sealed · commitment 843e7eae6b33eedc…30b542dc
# signatures only - the bodies are in the sealed source (see SEAL.md)
from __future__ import annotations

from typing import Any

def evaluate_creator(data: Any) -> dict | None: ...

def evaluate_token(chain: dict | None, creator: str | None = ...) -> dict | None:
    """SolanaRPC.holders() -> checks, failures and the shares behind them."""
    ...

def fail_reason(token: dict) -> str | None:
    """The first failure in words, for the message and the log."""
    ...

class Guard:
    def __init__(self, api, rpc = ..., page: int = ...): ...
    @staticmethod
    def evaluate(data: Any) -> dict | None: ...
    async def creator(self, wallet: str | None) -> dict | None:
        """The creator's record, or None if pump.fun did not answer."""
        ...
    @property
    def onchain(self) -> bool: ...
    async def token(self, mint: str | None, creator: str | None = ...) -> dict | None:
        """The on-chain checks, or None when they are off or no RPC endpoint answered."""
        ...
