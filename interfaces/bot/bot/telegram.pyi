"""Starting the bot: the database, the sources, the command menu, the loops, long polling."""
# QuantumOrb · interface contract · quantumorb/bot/telegram.py
# implementation: sealed · commitment d81f1521e10011f9…d1c9b4ee
# signatures only - the bodies are in the sealed source (see SEAL.md)
from __future__ import annotations

from typing import Any

def make_sources() -> tuple[PumpFunAPI, SolanaRPC | None]: ...

async def run() -> None: ...
