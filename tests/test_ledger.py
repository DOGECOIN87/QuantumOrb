"""The ledger: a chain that cannot be cut, reordered or rewritten without the check saying where."""
import datetime as dt
import json

import pytest
from conftest import SEED, TREE

from vault.ledger import ARCHIVE, CURRENT, GENESIS, LEDGER, ROOT_FILE, Ledger, record, verify_ledger
from vault.manifest import build

T0 = dt.datetime(2026, 10, 12, 21, 22, 22, tzinfo=dt.timezone.utc)


def three_seals(tree, showcase):
    record(showcase, build(tree, SEED, paths=list(TREE), now=T0), "first")
    (tree / "quantumorb/db.py").write_bytes(b"import sqlite3\n\nSCHEMA = 3\n")
    record(showcase, build(tree, SEED, paths=list(TREE), now=T0 + dt.timedelta(hours=2)), "second")
    (tree / "docs/NEW.md").write_bytes(b"# new\n")
    record(showcase, build(tree, SEED, paths=list(TREE) + ["docs/NEW.md"], now=T0 + dt.timedelta(days=1)), "third")
    return Ledger.load(showcase / LEDGER)


def test_a_clean_chain(tree, showcase):
    ledger = three_seals(tree, showcase)
    assert [e.n for e in ledger.entries] == [1, 2, 3]
    assert ledger.entries[0].prev == GENESIS
    assert ledger.entries[1].prev == ledger.entries[0].hash
    assert verify_ledger(ledger, showcase) == []
    assert (showcase / ROOT_FILE).read_text().strip() == ledger.head.root
    assert len(list((showcase / ARCHIVE).iterdir())) == 3


def test_the_published_ledger(repo):
    ledger = Ledger.load(repo / LEDGER)
    assert ledger.entries, "the repository publishes at least one seal"
    assert verify_ledger(ledger, repo) == []


def _rewrite(showcase, fn):
    d = json.loads((showcase / LEDGER).read_text())
    fn(d)
    (showcase / LEDGER).write_text(json.dumps(d))
    return Ledger.load(showcase / LEDGER)


@pytest.mark.parametrize("fn,message", [
    (lambda d: d["entries"][1].update(note="rewritten later"), "its hash"),
    (lambda d: d["entries"].pop(1), "does not point"),
    (lambda d: d["entries"].reverse(), "expected number"),
    (lambda d: d["entries"][2].update(lines=1), "its hash"),
    (lambda d: d.update(scheme="other"), "scheme"),
])
def test_tampering_is_named(tree, showcase, fn, message):
    three_seals(tree, showcase)
    assert any(message in p for p in verify_ledger(_rewrite(showcase, fn), showcase))


def test_a_rehashed_forgery_still_breaks_the_next_link(tree, showcase):
    ledger = three_seals(tree, showcase)

    def forge(d):
        e = d["entries"][1]
        e["note"] = "forged"
        from vault.ledger import Entry
        e["hash"] = Entry(**e).computed_hash()

    problems = verify_ledger(_rewrite(showcase, forge), showcase)
    assert any("entry 3: does not point" in p for p in problems)
    assert ledger.entries[1].note == "second"


def test_archives_are_held_to_their_entries(tree, showcase):
    ledger = three_seals(tree, showcase)
    (showcase / ledger.entries[0].archive).unlink()
    assert any("missing" in p for p in verify_ledger(ledger, showcase))


def test_the_head_must_be_current(tree, showcase):
    ledger = three_seals(tree, showcase)
    (showcase / ROOT_FILE).write_text(ledger.entries[0].root + "\n")
    assert any("sealed/ROOT" in p for p in verify_ledger(ledger, showcase))
    m = json.loads((showcase / CURRENT).read_text())
    m["root"] = ledger.entries[0].root
    (showcase / CURRENT).write_text(json.dumps(m))
    assert any("current seal" in p for p in verify_ledger(ledger, showcase))


def test_append_refuses_a_repeat_or_a_seal_from_the_past(tree, showcase):
    record(showcase, build(tree, SEED, paths=list(TREE), now=T0), "first")
    with pytest.raises(ValueError, match="already"):
        record(showcase, build(tree, SEED, paths=list(TREE), now=T0 + dt.timedelta(hours=1)), "again")
    (tree / "docs/STATUS.md").write_bytes(b"# moved\n")
    with pytest.raises(ValueError, match="older"):
        record(showcase, build(tree, SEED, paths=list(TREE), now=T0 - dt.timedelta(hours=1)), "back in time")
