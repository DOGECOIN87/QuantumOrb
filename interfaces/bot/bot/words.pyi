"""Everything the bot says that is not built from data: the welcome, the help, Signals, prompts."""
# QuantumOrb · interface contract · quantumorb/bot/words.py
# implementation: sealed · commitment 927e9749493205ee…9a05dc58
# signatures only - the bodies are in the sealed source (see SEAL.md)
from __future__ import annotations

from typing import Any

def welcome_text() -> str:
    """The caption under the picture on /start."""
    ...

def help_text() -> str: ...

def signals_text(on_list: bool) -> str: ...

def hot_text(on: bool, wallets: int) -> str: ...
