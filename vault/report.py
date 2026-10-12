"""The README's seal figures, generated from the seal itself so they can never drift from it.

The README carries three generated blocks between markers:

    <!-- vault:seal -->        root, date, totals
    <!-- vault:components -->  the table by component
    <!-- vault:ledger -->      every seal so far

`render_blocks` builds them; `sync` rewrites them in place; `check` reports a README that no longer says
what the seal says. CI runs the check.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from .ledger import Ledger
from .manifest import Manifest

BLOCKS = ("seal", "components", "ledger")


def kb(n: int) -> str:
    return f"{round(n / 1024):,} KB"


def size(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else kb(n)


def seal_block(m: Manifest) -> str:
    s = m.source
    when = m.sealed.strftime("%Y-%m-%d %H:%M UTC")
    return ("```\n"
            f"root      {m.root}\n"
            f"sealed    {when}\n"
            f"files     {s.files}        lines     {s.lines:,}        size     {size(s.bytes)}\n"
            "```")


def components_block(m: Manifest) -> str:
    rows = ["| Component | Files | Lines | Size |", "|---|---:|---:|---:|"]
    rows += [f"| {label} | {t.files} | {t.lines:,} | {kb(t.bytes)} |" for label, t in m.groups()]
    return "\n".join(rows)


def ledger_block(ledger: Ledger) -> str:
    rows = ["| # | Sealed (UTC) | Files | Lines | Δ lines | Root | Note |", "|---:|---|---:|---:|---:|---|---|"]
    prev = None
    for e in ledger.entries:
        delta = "" if prev is None else f"{e.lines - prev.lines:+,}"
        when = e.sealed_at.replace("T", " ").replace("Z", "")[:16]
        rows.append(f"| {e.n} | {when} | {e.files} | {e.lines:,} | {delta} | `{e.root[:16]}…` | {e.note} |")
        prev = e
    return "\n".join(rows)


def render_blocks(m: Manifest, ledger: Optional[Ledger] = None) -> Dict[str, str]:
    out = {"seal": seal_block(m), "components": components_block(m)}
    if ledger is not None and ledger.entries:
        out["ledger"] = ledger_block(ledger)
    return out


def _pattern(name: str) -> "re.Pattern[str]":
    # an empty block (just the two markers) is valid: that is how a new one is added
    return re.compile(rf"(<!-- vault:{name} -->)\n(.*?)\n?(<!-- /vault:{name} -->)", re.S)


def sync(readme: str, blocks: Dict[str, str]) -> str:
    for name, body in blocks.items():
        pat = _pattern(name)
        if not pat.search(readme):
            raise KeyError(f"the README has no vault:{name} block")
        readme = pat.sub(lambda mt, body=body: mt.group(1) + "\n" + body + "\n" + mt.group(3), readme)
    return readme


def check(readme: str, blocks: Dict[str, str]) -> List[str]:
    problems = []
    for name, body in blocks.items():
        mt = _pattern(name).search(readme)
        if mt is None:
            problems.append(f"README: no vault:{name} block")
        elif mt.group(2) != body:
            problems.append(f"README: the vault:{name} block is out of date (python -m vault report --write)")
    return problems
