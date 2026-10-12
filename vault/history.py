"""What git says about the seals: when each root first arrived in this repository.

The ledger is this repository's own account of its seals. Git is the independent one - every commit that
touched `sealed/ROOT` is dated, and the hashes chain back to the first commit. This module reads that
account and holds the ledger to it: every root in the ledger was published, in the ledger's order, and none
was published before it was sealed.

On a shallow clone (CI's default) there is no history to read; the check says so instead of passing.
"""
from __future__ import annotations

import datetime as dt
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .ledger import Ledger
from .manifest import parse_timestamp


@dataclass(frozen=True)
class Published:
    commit: str
    date: dt.datetime
    root: str


def _git(repo: Path, *args: str) -> Optional[str]:
    try:
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def published_roots(repo: Path) -> Optional[List[Published]]:
    """Each distinct root in the order git first saw it, or None when there is no full history to read."""
    repo = Path(repo)
    if (_git(repo, "rev-parse", "--is-shallow-repository") or "").strip() != "false":
        return None
    log = _git(repo, "log", "--reverse", "--format=%H %cI", "--", "sealed/ROOT")
    if log is None:
        return None
    seen, out = set(), []
    for line in log.splitlines():
        commit, _, when = line.partition(" ")
        root = (_git(repo, "show", f"{commit}:sealed/ROOT") or "").strip()
        if not root or root in seen:
            continue
        seen.add(root)
        out.append(Published(commit=commit, date=dt.datetime.fromisoformat(when.strip()), root=root))
    return out


def check_history(ledger: Ledger, published: List[Published]) -> List[str]:
    problems = []
    first = {p.root: p for p in published}
    order = [p.root for p in published]
    last_index = -1
    for e in ledger.entries:
        p = first.get(e.root)
        if p is None:
            problems.append(f"ledger entry {e.n}: root {e.root[:12]} was never committed to sealed/ROOT")
            continue
        if p.date < parse_timestamp(e.sealed_at):
            problems.append(f"ledger entry {e.n}: published at {p.date.isoformat()}, before it was sealed")
        idx = order.index(e.root)
        if idx < last_index:
            problems.append(f"ledger entry {e.n}: published out of order")
        last_index = idx
    return problems


def timeline(ledger: Ledger, published: Optional[List[Published]]) -> List[dict]:
    first = {p.root: p for p in (published or [])}
    rows, prev = [], None
    for e in ledger.entries:
        p = first.get(e.root)
        rows.append({
            "n": e.n, "sealed_at": e.sealed_at, "root": e.root, "files": e.files, "lines": e.lines,
            "lines_delta": None if prev is None else e.lines - prev.lines,
            "published": p.date.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if p else None,
            "commit": p.commit[:12] if p else None, "note": e.note,
        })
        prev = e
    return rows
