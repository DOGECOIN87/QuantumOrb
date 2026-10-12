"""Comparing seals, and opening files: what moved is visible, how it moved is not."""
import datetime as dt
import json

import pytest
from conftest import SEED, TREE

from vault.diff import diff, render
from vault.manifest import build
from vault.opened import BAD_SALT, MATCH, MISMATCH, MISSING, NOT_SEALED, SALTS, check_opened, open_files

T0 = dt.datetime(2026, 10, 12, 21, 0, 0, tzinfo=dt.timezone.utc)


def test_diff_names_files_not_content(tree):
    old = build(tree, SEED, paths=list(TREE), now=T0)
    (tree / "quantumorb/db.py").write_bytes(b"import sqlite3\n\nSCHEMA = 3\nMIGRATIONS = 3\n")
    (tree / "quantumorb/pipeline/guard.py").write_bytes(b"def check():\n    ...\n")
    paths = [p for p in TREE if p != "docs/STATUS.md"] + ["quantumorb/pipeline/guard.py"]
    new = build(tree, SEED, paths=paths, now=T0 + dt.timedelta(hours=1))
    d = diff(old, new)
    assert d.changed == ["quantumorb/db.py"]
    assert d.added == ["quantumorb/pipeline/guard.py"]
    assert d.removed == ["docs/STATUS.md"]
    assert d.unchanged == len(TREE) - 2
    assert d.by_component["bot · core"] == {"added": 0, "removed": 0, "changed": 1}
    assert d.by_component["bot · pipeline"]["added"] == 1
    assert d.lines_delta == new.source.lines - old.source.lines
    text = render(d, paths=True)
    assert "1 changed · 1 added · 1 removed" in text and "~ quantumorb/db.py" in text
    assert "SCHEMA" not in text


def test_a_new_seed_changes_every_commitment(tree):
    a = build(tree, SEED, paths=list(TREE), now=T0)
    b = build(tree, bytes(32) + b"\x01", paths=list(TREE), now=T0)
    assert len(diff(a, b).changed) == len(TREE)


def test_open_then_check(tree, tmp_path):
    m = build(tree, SEED, paths=list(TREE), now=T0)
    out = tmp_path / "opened"
    assert open_files(m, tree, SEED, out, ["quantumorb/db.py", "site/server/index.js"]) == [
        "quantumorb/db.py", "site/server/index.js"]
    salts = json.loads((out / SALTS).read_text())
    assert set(salts) == {"quantumorb/db.py", "site/server/index.js"}
    assert [r.status for r in check_opened(m, out)] == [MATCH, MATCH]


def test_every_way_an_opened_file_can_be_wrong(tree, tmp_path):
    m = build(tree, SEED, paths=list(TREE), now=T0)
    out = tmp_path / "opened"
    open_files(m, tree, SEED, out, ["quantumorb/db.py", "tests/test_db.py", "docs/STATUS.md"])
    (out / "quantumorb/db.py").write_bytes(b"import sqlite3\n\nSCHEMA = 99\n")       # one byte moved
    (out / "tests/test_db.py").unlink()                                                # gone
    salts = json.loads((out / SALTS).read_text())
    salts["docs/STATUS.md"] = "nothex"                                                 # malformed
    salts["quantumorb/secret.py"] = "0" * 64                                           # never sealed
    salts["../../etc/passwd"] = "0" * 64                                               # outside
    (out / SALTS).write_text(json.dumps(salts))
    got = {r.path: r.status for r in check_opened(m, out)}
    assert got == {"quantumorb/db.py": MISMATCH, "tests/test_db.py": MISSING, "docs/STATUS.md": BAD_SALT,
                   "quantumorb/secret.py": NOT_SEALED, "../../etc/passwd": NOT_SEALED}


def test_a_changed_file_cannot_be_opened_as_sealed(tree, tmp_path):
    m = build(tree, SEED, paths=list(TREE), now=T0)
    (tree / "quantumorb/db.py").write_bytes(b"changed after the seal\n")
    with pytest.raises(ValueError, match="changed since the seal"):
        open_files(m, tree, SEED, tmp_path / "o", ["quantumorb/db.py"])
    with pytest.raises(KeyError):
        open_files(m, tree, SEED, tmp_path / "o", ["not/sealed.py"])
