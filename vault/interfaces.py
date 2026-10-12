"""The interface contracts in `interfaces/`, held to the seal they were cut from.

Every contract opens with the sealed path it describes and the first and last hex of that file's
commitment. This module reads those headers, lists the public names each contract declares, and checks
that every contract still names a file - and a commitment - that is in the current seal. A contract whose
source has changed since it was cut fails here until it is cut again.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .manifest import Manifest

HEADER = re.compile(r"interface contract · (?P<source>\S+)")
COMMIT = re.compile(r"commitment (?P<head>[0-9a-f]{16})…(?P<tail>[0-9a-f]{8})")
DTS_EXPORT = re.compile(
    r"^export\s+(?:declare\s+)?(?:async\s+)?(?P<kind>function|const|let|class|interface|type|enum)\s+(?P<name>[A-Za-z_$][\w$]*)",
    re.M)
KINDS = {".pyi": "python", ".ts": "typescript"}


@dataclass
class Contract:
    file: str
    source: str
    head: str
    tail: str
    language: str
    names: List[str] = field(default_factory=list)
    error: Optional[str] = None

    def matches(self, commitment: str) -> bool:
        return commitment.startswith(self.head) and commitment.endswith(self.tail)


def public_names_pyi(text: str) -> List[str]:
    """Top-level classes and functions, and the public methods of each class as Class.method."""
    tree = ast.parse(text)
    names: List[str] = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("_"):
            names.append(n.name)
        elif isinstance(n, ast.ClassDef) and not n.name.startswith("_"):
            names.append(n.name)
            names += [f"{n.name}.{m.name}" for m in n.body
                      if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and not m.name.startswith("_")]
    return names


def public_names_dts(text: str) -> List[str]:
    return [m.group("name") for m in DTS_EXPORT.finditer(text)]


def read_contract(path: Path, root: Path) -> Optional[Contract]:
    text = path.read_text(encoding="utf-8")
    head = "\n".join(text.splitlines()[:6])
    h, c = HEADER.search(head), COMMIT.search(head)
    if not (h and c):
        return None
    language = "python" if path.suffix == ".pyi" else "typescript"
    contract = Contract(file=path.relative_to(root).as_posix(), source=h.group("source"), head=c.group("head"),
                        tail=c.group("tail"), language=language)
    try:
        contract.names = public_names_pyi(text) if language == "python" else public_names_dts(text)
    except SyntaxError as e:
        contract.error = f"does not parse: {e.msg} (line {e.lineno})"
    return contract


def scan(root: Path) -> List[Contract]:
    root = Path(root)
    files = sorted(list(root.rglob("*.pyi")) + list(root.rglob("*.d.ts")))
    return [c for c in (read_contract(f, root) for f in files) if c is not None]


def audit(contracts: List[Contract], manifest: Manifest, root: Optional[Path] = None) -> List[str]:
    problems: List[str] = []
    sealed = manifest.by_path()
    seen: Dict[str, str] = {}
    if root is not None:
        cut = {c.file for c in contracts}
        for f in sorted(list(Path(root).rglob("*.pyi")) + list(Path(root).rglob("*.d.ts"))):
            if f.relative_to(root).as_posix() not in cut:
                problems.append(f"{f.relative_to(root).as_posix()}: no contract header")
    for c in contracts:
        if c.error:
            problems.append(f"{c.file}: {c.error}")
        entry = sealed.get(c.source)
        if entry is None:
            problems.append(f"{c.file}: names {c.source}, which is not in the seal")
        elif not c.matches(entry.commitment):
            problems.append(f"{c.file}: cut from a different version of {c.source} than the one sealed")
        if c.source in seen:
            problems.append(f"{c.file}: {c.source} already has a contract ({seen[c.source]})")
        seen[c.source] = c.file
        if not c.names:
            problems.append(f"{c.file}: declares nothing")
    return problems


def index_markdown(contracts: List[Contract], manifest: Manifest) -> str:
    sealed = manifest.by_path()
    rows = ["| Contract | Sealed source | Component | Public names | Commitment |", "|---|---|---|---:|---|"]
    for c in sorted(contracts, key=lambda c: c.file):
        e = sealed.get(c.source)
        rows.append(f"| [`{c.file}`]({c.file}) | `{c.source}` | {e.component if e else '-'} | {len(c.names)} "
                    f"| `{c.head}…{c.tail}` |")
    total = sum(len(c.names) for c in contracts)
    langs = sorted({c.language for c in contracts})
    return (f"{len(contracts)} contracts · {total} public names · {', '.join(langs)} · "
            f"seal `{manifest.root[:16]}…`\n\n" + "\n".join(rows) + "\n")
