"""Opened files: a sealed file published together with its salt, checked against the day of the seal.

    opened/
      salts.json             {"path": "salt as hex", ...}
      quantumorb/db.py       the file, byte for byte as it was sealed

Opening a file publishes that file's salt and nothing else - the seed stays where it is, and every other
commitment stays as closed as it was.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List

from .manifest import Manifest
from .scheme import commitment, is_hex64, salt_for

SALTS = "salts.json"

MATCH = "match"
MISMATCH = "mismatch"
NOT_SEALED = "not sealed"
MISSING = "missing"
BAD_SALT = "bad salt"


@dataclass(frozen=True)
class Opened:
    path: str
    status: str

    @property
    def ok(self) -> bool:
        return self.status == MATCH

    def describe(self) -> str:
        return {
            MATCH: f"{self.path} matches its commitment",
            MISMATCH: f"opened file {self.path} does not match its commitment",
            NOT_SEALED: f"opened file {self.path} is not in the seal",
            MISSING: f"opened file {self.path} is listed in {SALTS} but not present",
            BAD_SALT: f"opened file {self.path} has a malformed salt",
        }[self.status]


def load_salts(base: Path) -> Dict[str, str]:
    f = Path(base) / SALTS
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def _inside(base: Path, path: str) -> Path:
    target = (base / path).resolve()
    if base.resolve() not in target.parents:
        raise ValueError(f"{path} points outside {base}")
    return target


def check_opened(manifest: Manifest, base: Path) -> List[Opened]:
    base = Path(base)
    sealed = manifest.by_path()
    out: List[Opened] = []
    for path, salt_hex in sorted(load_salts(base).items()):
        entry = sealed.get(path)
        if entry is None:
            out.append(Opened(path, NOT_SEALED))
            continue
        if not is_hex64(salt_hex):
            out.append(Opened(path, BAD_SALT))
            continue
        try:
            target = _inside(base, path)
        except ValueError:
            out.append(Opened(path, NOT_SEALED))
            continue
        if not target.is_file():
            out.append(Opened(path, MISSING))
            continue
        ok = commitment(bytes.fromhex(salt_hex), target.read_bytes()) == entry.commitment
        out.append(Opened(path, MATCH if ok else MISMATCH))
    return out


def open_files(manifest: Manifest, src: Path, seed: bytes, out: Path, paths: Iterable[str]) -> List[str]:
    """Private side: copy the named files out of the tree with their salts, ready to publish."""
    src, out = Path(src).resolve(), Path(out)
    sealed = manifest.by_path()
    salts = load_salts(out)
    done = []
    for p in paths:
        if p not in sealed:
            raise KeyError(f"{p} is not in the seal")
        content = (src / p).read_bytes()
        salt = salt_for(seed, p)
        if commitment(salt, content) != sealed[p].commitment:
            raise ValueError(f"{p} changed since the seal; open the sealed version (git show) instead")
        (out / p).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src / p, out / p)
        salts[p] = salt.hex()
        done.append(p)
    out.mkdir(parents=True, exist_ok=True)
    (out / SALTS).write_text(json.dumps(dict(sorted(salts.items())), indent=1) + "\n", encoding="utf-8")
    return done
