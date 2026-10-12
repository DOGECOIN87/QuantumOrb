"""The ledger: every seal this repository has published, in order, each one bound to the one before it.

A root on its own says what the code was on one day. A ledger says how it got there: entry n carries the
hash of entry n-1, so no seal can be dropped, reordered or rewritten later without every following hash
changing. Each entry's full manifest is kept in `sealed/archive/`, so a proof can still be cut against any
earlier root, and two seals can be compared file by file.

    entry.hash = SHA-256("qorb-seal-v1/ledger/" || canonical JSON of the entry without its hash)
    canonical  = keys sorted, no whitespace, UTF-8
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional

from .manifest import Manifest, ManifestError, parse_timestamp
from .scheme import SCHEME, is_hex64

GENESIS = "0" * 64
ARCHIVE = Path("sealed") / "archive"
LEDGER = Path("sealed") / "ledger.json"
CURRENT = Path("sealed") / "manifest.json"
ROOT_FILE = Path("sealed") / "ROOT"


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@dataclass(frozen=True)
class Entry:
    n: int
    sealed_at: str
    root: str
    files: int
    lines: int
    bytes: int
    note: str
    prev: str
    hash: str = ""

    def body(self) -> dict:
        d = asdict(self)
        d.pop("hash")
        return d

    def computed_hash(self) -> str:
        return hashlib.sha256(f"{SCHEME}/ledger/".encode() + canonical(self.body())).hexdigest()

    def sealed(self) -> "Entry":
        return Entry(**{**self.body(), "hash": self.computed_hash()})

    @property
    def archive(self) -> Path:
        return ARCHIVE / f"{self.n:04d}-{self.root[:12]}.json"


@dataclass
class Ledger:
    entries: List[Entry]
    scheme: str = SCHEME

    @classmethod
    def load(cls, path) -> "Ledger":
        p = Path(path)
        if not p.exists():
            return cls(entries=[])
        d = json.loads(p.read_text(encoding="utf-8"))
        return cls(scheme=d.get("scheme", ""), entries=[Entry(**e) for e in d.get("entries", [])])

    def dumps(self) -> str:
        return json.dumps({"scheme": self.scheme, "entries": [asdict(e) for e in self.entries]},
                          indent=1, ensure_ascii=False) + "\n"

    @property
    def head(self) -> Optional[Entry]:
        return self.entries[-1] if self.entries else None

    def append(self, manifest: Manifest, note: str) -> Entry:
        """The next entry for a new seal. The caller writes the ledger and the archive."""
        head = self.head
        if head and head.root == manifest.root:
            raise ValueError("this root is already the head of the ledger")
        if head and manifest.sealed <= parse_timestamp(head.sealed_at):
            raise ValueError("a seal cannot be older than the one before it")
        t = manifest.source
        entry = Entry(n=(head.n + 1 if head else 1), sealed_at=manifest.sealed_at, root=manifest.root,
                      files=t.files, lines=t.lines, bytes=t.bytes, note=note,
                      prev=(head.hash if head else GENESIS)).sealed()
        self.entries.append(entry)
        return entry


def verify_ledger(ledger: Ledger, base: Path) -> List[str]:
    """The chain holds, every archived manifest is intact and matches its entry, and the head is current."""
    base = Path(base)
    problems: List[str] = []
    if ledger.scheme != SCHEME:
        problems.append(f"ledger scheme {ledger.scheme!r} is not {SCHEME}")
    if not ledger.entries:
        return problems + ["the ledger is empty"]
    prev_hash, prev_time = GENESIS, None
    for i, e in enumerate(ledger.entries, start=1):
        tag = f"ledger entry {e.n}"
        if e.n != i:
            problems.append(f"{tag}: expected number {i}")
        if e.prev != prev_hash:
            problems.append(f"{tag}: does not point at the entry before it")
        if not (is_hex64(e.hash) and e.hash == e.computed_hash()):
            problems.append(f"{tag}: its hash does not match its contents")
        try:
            t = parse_timestamp(e.sealed_at)
            if prev_time and t <= prev_time:
                problems.append(f"{tag}: sealed before the entry it follows")
            prev_time = t
        except ValueError:
            problems.append(f"{tag}: sealed_at {e.sealed_at!r} is malformed")
        archive = base / e.archive
        if not archive.exists():
            problems.append(f"{tag}: archived manifest {e.archive.as_posix()} is missing")
        else:
            try:
                m = Manifest.load(archive)
            except (ManifestError, ValueError, KeyError) as ex:
                problems.append(f"{tag}: archived manifest is not a valid seal ({ex})")
            else:
                s = m.source
                if (m.root, m.sealed_at, s.files, s.lines, s.bytes) != (e.root, e.sealed_at, e.files, e.lines, e.bytes):
                    problems.append(f"{tag}: archived manifest does not match the entry")
        prev_hash = e.hash
    head = ledger.entries[-1]
    current = base / CURRENT
    if current.exists():
        if json.loads(current.read_text(encoding="utf-8")).get("root") != head.root:
            problems.append("the ledger's head is not the current seal")
    root_file = base / ROOT_FILE
    if root_file.exists() and root_file.read_text().strip() != head.root:
        problems.append("the ledger's head is not sealed/ROOT")
    return problems


def record(base: Path, manifest: Manifest, note: str) -> Entry:
    """Publish a new seal: manifest, ROOT, archive copy and ledger entry, in one step."""
    base = Path(base)
    ledger = Ledger.load(base / LEDGER)
    entry = ledger.append(manifest, note)
    text = manifest.dumps()
    (base / ARCHIVE).mkdir(parents=True, exist_ok=True)
    (base / entry.archive).write_text(text, encoding="utf-8")
    (base / CURRENT).write_text(text, encoding="utf-8")
    (base / ROOT_FILE).write_text(manifest.root + "\n", encoding="utf-8")
    (base / LEDGER).write_text(ledger.dumps(), encoding="utf-8")
    return entry
