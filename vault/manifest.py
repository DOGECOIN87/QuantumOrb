"""A seal's manifest: every file of the private tree as a path, a size, a line count and a commitment.

Two ways in. `verify_manifest` takes the JSON as it is and lists everything that does not add up - it is
what CI runs, and it never raises on a bad file, it reports it. `Manifest.from_dict` is strict: it builds
typed entries and refuses anything malformed, for the tools that go on to cut proofs or compare seals.
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .merkle import MerkleTree, Proof
from .scheme import SCHEME, commitment, count_lines, is_hex64, leaf, salt_for

COMPONENTS: Sequence[Tuple[str, str]] = (  # (path prefix, component) - first match wins
    ("quantumorb/sources/", "bot · sources"),
    ("quantumorb/pipeline/", "bot · pipeline"),
    ("quantumorb/bot/", "bot · interface"),
    ("quantumorb/", "bot · core"),
    ("tests/fixtures/", "bot · fixtures"),
    ("tests/", "bot · tests"),
    ("site/server/", "observatory · server"),
    ("site/client/", "observatory · client"),
    ("site/public/", "observatory · public"),
    ("site/", "observatory · build & tests"),
    ("deploy/", "infrastructure"),
    ("docker-compose.yml", "infrastructure"),
    (".dockerignore", "infrastructure"),
    (".github/", "infrastructure"),
    ("docs/", "documentation"),
    ("scripts/", "tooling"),
    ("assets/", "brand"),
)
FALLBACK_COMPONENT = "repository"

# how the README groups the components, in this order
GROUPS: Sequence[Tuple[str, Sequence[str]]] = (
    ("bot · sources", ("bot · sources",)),
    ("bot · pipeline", ("bot · pipeline",)),
    ("bot · interface", ("bot · interface",)),
    ("bot · core", ("bot · core",)),
    ("bot · tests + fixtures", ("bot · tests", "bot · fixtures")),
    ("observatory · server", ("observatory · server",)),
    ("observatory · client", ("observatory · client",)),
    ("observatory · public", ("observatory · public",)),
    ("observatory · build & tests", ("observatory · build & tests",)),
    ("infrastructure", ("infrastructure",)),
    ("documentation, tooling, brand, repository", ("documentation", "tooling", "brand", FALLBACK_COMPONENT)),
)

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class ManifestError(ValueError):
    pass


def component_of(path: str) -> str:
    for prefix, name in COMPONENTS:
        if path == prefix or path.startswith(prefix):
            return name
    return FALLBACK_COMPONENT


def parse_timestamp(value: str) -> dt.datetime:
    return dt.datetime.strptime(value, TIMESTAMP_FORMAT).replace(tzinfo=dt.timezone.utc)


@dataclass(frozen=True)
class Entry:
    path: str
    component: str
    lines: int
    bytes: int
    commitment: str

    @property
    def leaf(self) -> bytes:
        return leaf(self.path, self.commitment)

    def to_dict(self) -> dict:
        return {"path": self.path, "component": self.component, "lines": self.lines, "bytes": self.bytes,
                "commitment": self.commitment}


@dataclass(frozen=True)
class Totals:
    files: int = 0
    lines: int = 0
    bytes: int = 0

    def to_dict(self) -> dict:
        return {"files": self.files, "lines": self.lines, "bytes": self.bytes}

    def __add__(self, other: "Totals") -> "Totals":
        return Totals(self.files + other.files, self.lines + other.lines, self.bytes + other.bytes)


def totals_of(entries: Iterable[Entry]) -> Totals:
    t = Totals()
    for e in entries:
        t = t + Totals(1, e.lines, e.bytes)
    return t


@dataclass
class Manifest:
    scheme: str
    sealed_at: str
    root: str
    files: List[Entry] = field(default_factory=list)

    # ------------------------------------------------------------------ reading
    @classmethod
    def from_dict(cls, d: dict) -> "Manifest":
        for key in ("scheme", "sealed_at", "source", "root", "files"):
            if key not in d:
                raise ManifestError(f"the manifest has no {key!r}")
        if d["scheme"] != SCHEME:
            raise ManifestError(f"unknown scheme {d['scheme']!r}")
        try:
            parse_timestamp(d["sealed_at"])
        except (TypeError, ValueError) as e:
            raise ManifestError(f"sealed_at {d['sealed_at']!r} is not {TIMESTAMP_FORMAT}") from e
        files = []
        for row in d["files"]:
            try:
                e = Entry(path=row["path"], component=row["component"], lines=row["lines"], bytes=row["bytes"],
                          commitment=row["commitment"])
            except (KeyError, TypeError) as ex:
                raise ManifestError(f"a malformed file row: {row!r}") from ex
            if not isinstance(e.path, str) or not e.path or e.path.startswith("/") or ".." in e.path.split("/"):
                raise ManifestError(f"a path that does not stay inside the tree: {e.path!r}")
            if not is_hex64(e.commitment):
                raise ManifestError(f"bad commitment for {e.path}")
            if not (isinstance(e.lines, int) and isinstance(e.bytes, int)) or e.lines < 0 or e.bytes < 0:
                raise ManifestError(f"bad size for {e.path}")
            files.append(e)
        m = cls(scheme=d["scheme"], sealed_at=d["sealed_at"], root=d["root"], files=files)
        problems = verify_manifest(d, None, check_published=False)
        if problems:
            raise ManifestError("; ".join(problems))
        return m

    @classmethod
    def load(cls, path) -> "Manifest":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    # ------------------------------------------------------------------ figures
    @property
    def source(self) -> Totals:
        return totals_of(self.files)

    def components(self) -> Dict[str, Totals]:
        out: Dict[str, Totals] = {}
        for e in self.files:
            out[e.component] = out.get(e.component, Totals()) + Totals(1, e.lines, e.bytes)
        return dict(sorted(out.items()))

    def groups(self) -> List[Tuple[str, Totals]]:
        comps = self.components()
        rows = [(label, sum((comps.get(c, Totals()) for c in members), Totals())) for label, members in GROUPS]
        known = {c for _, members in GROUPS for c in members}
        rows += [(c, t) for c, t in comps.items() if c not in known]
        return [(label, t) for label, t in rows if t.files]

    def by_path(self) -> Dict[str, Entry]:
        return {e.path: e for e in self.files}

    @property
    def sealed(self) -> dt.datetime:
        return parse_timestamp(self.sealed_at)

    # ------------------------------------------------------------------ the tree
    def tree(self) -> MerkleTree:
        return MerkleTree([e.leaf for e in self.files])

    def proof(self, path: str) -> Proof:
        index = next((i for i, e in enumerate(self.files) if e.path == path), None)
        if index is None:
            raise KeyError(f"{path} is not in the seal")
        return Proof(path=path, commitment=self.files[index].commitment, root=self.root,
                     steps=tuple(self.tree().path(index)))

    def to_dict(self) -> dict:
        return {
            "scheme": self.scheme,
            "sealed_at": self.sealed_at,
            "source": self.source.to_dict(),
            "components": {k: v.to_dict() for k, v in self.components().items()},
            "root": self.root,
            "files": [e.to_dict() for e in self.files],
        }

    def dumps(self) -> str:
        return json.dumps(self.to_dict(), indent=1, ensure_ascii=False) + "\n"


def verify_manifest(m: dict, published_root: Optional[str], check_published: bool = True) -> List[str]:
    """Everything that can be checked without a single sealed file. [] when consistent."""
    problems: List[str] = []
    if m.get("scheme") != SCHEME:
        problems.append(f"unknown scheme {m.get('scheme')!r}")
    files = m.get("files") or []
    paths = [f.get("path") for f in files]
    if paths != sorted(paths) or len(set(paths)) != len(paths):
        problems.append("files are not sorted by path, or a path repeats")
    for f in files:
        if not is_hex64(f.get("commitment")):
            problems.append(f"bad commitment for {f.get('path')}")
    if problems and any(p.startswith("bad commitment") for p in problems):
        return problems
    root = MerkleTree([leaf(f["path"], f["commitment"]) for f in files]).root
    if root != m.get("root"):
        problems.append(f"manifest root {m.get('root')} != recomputed {root}")
    if check_published and published_root != root:
        problems.append(f"sealed/ROOT {published_root} != recomputed {root}")
    totals = {"files": len(files), "lines": sum(f["lines"] for f in files), "bytes": sum(f["bytes"] for f in files)}
    if totals != m.get("source"):
        problems.append(f"totals {m.get('source')} != recomputed {totals}")
    if "components" in m:
        comps: Dict[str, dict] = {}
        for f in files:
            c = comps.setdefault(f["component"], {"files": 0, "lines": 0, "bytes": 0})
            c["files"] += 1
            c["lines"] += f["lines"]
            c["bytes"] += f["bytes"]
        if dict(sorted(comps.items())) != m["components"]:
            problems.append("component totals do not add up to the files")
    for f in files:
        if f.get("component") != component_of(f["path"]):
            want = component_of(f["path"])
            problems.append(f"{f['path']} is filed under {f.get('component')!r}, the scheme says {want!r}")
            break
    if "sealed_at" in m:
        try:
            parse_timestamp(m["sealed_at"])
        except (TypeError, ValueError):
            problems.append(f"sealed_at {m['sealed_at']!r} is not {TIMESTAMP_FORMAT}")
    return problems


# ---------------------------------------------------------------------- private side

def tracked_files(src: Path) -> List[str]:
    out = subprocess.run(["git", "-C", str(src), "ls-files", "-z"], capture_output=True, check=True).stdout
    return sorted(p for p in out.decode("utf-8").split("\0") if p)


def build(src: Path, seed: bytes, paths: Optional[Sequence[str]] = None,
          now: Optional[dt.datetime] = None) -> Manifest:
    """Seal a tree: the git-tracked files of `src`, unless `paths` names them. Needs the seed."""
    src = Path(src).resolve()
    entries = []
    for p in sorted(paths if paths is not None else tracked_files(src)):
        content = (src / p).read_bytes()
        entries.append(Entry(path=p, component=component_of(p), lines=count_lines(content), bytes=len(content),
                             commitment=commitment(salt_for(seed, p), content)))
    when = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc).strftime(TIMESTAMP_FORMAT)
    return Manifest(scheme=SCHEME, sealed_at=when, root=MerkleTree([e.leaf for e in entries]).root, files=entries)
