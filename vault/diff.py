"""Two seals side by side: which sealed files appeared, went, changed or stayed.

The salt of a file depends only on the seed and its path, so a file that did not change between two seals
keeps its commitment, and one that did gets a new one. That is enough to say *which* files moved and by
how many lines - and nothing about *how*.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from .manifest import Manifest


@dataclass
class SealDiff:
    old_root: str
    new_root: str
    added: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    changed: List[str] = field(default_factory=list)
    unchanged: int = 0
    lines_delta: int = 0
    bytes_delta: int = 0
    by_component: Dict[str, Dict[str, int]] = field(default_factory=dict)

    @property
    def touched(self) -> int:
        return len(self.added) + len(self.removed) + len(self.changed)

    def _bump(self, component: str, kind: str) -> None:
        row = self.by_component.setdefault(component, {"added": 0, "removed": 0, "changed": 0})
        row[kind] += 1


def diff(old: Manifest, new: Manifest) -> SealDiff:
    a, b = old.by_path(), new.by_path()
    d = SealDiff(old_root=old.root, new_root=new.root)
    for path in sorted(set(a) | set(b)):
        if path not in a:
            d.added.append(path)
            d._bump(b[path].component, "added")
        elif path not in b:
            d.removed.append(path)
            d._bump(a[path].component, "removed")
        elif a[path].commitment != b[path].commitment:
            d.changed.append(path)
            d._bump(b[path].component, "changed")
        else:
            d.unchanged += 1
    d.lines_delta = new.source.lines - old.source.lines
    d.bytes_delta = new.source.bytes - old.source.bytes
    d.by_component = dict(sorted(d.by_component.items()))
    return d


def render(d: SealDiff, paths: bool = False) -> str:
    out = [f"{d.old_root[:12]} -> {d.new_root[:12]}",
           f"  {len(d.changed)} changed · {len(d.added)} added · {len(d.removed)} removed · {d.unchanged} unchanged"
           f" · lines {d.lines_delta:+,} · bytes {d.bytes_delta:+,}"]
    for comp, row in d.by_component.items():
        parts = [f"{v} {k}" for k, v in row.items() if v]
        out.append(f"  {comp:<32} {' · '.join(parts)}")
    if paths:
        for mark, group in (("~", d.changed), ("+", d.added), ("-", d.removed)):
            out += [f"  {mark} {p}" for p in group]
    return "\n".join(out)
