"""The manifest model: built from a tree, strict on the way in, every figure recomputed."""
import datetime as dt
import json
import shutil
import subprocess

import pytest
from conftest import SEED, TREE

from vault.manifest import GROUPS, Manifest, ManifestError, build, component_of, tracked_files, verify_manifest

NOW = dt.datetime(2026, 10, 12, 21, 22, 22, tzinfo=dt.timezone.utc)


def test_build_and_reload(tree):
    m = build(tree, SEED, paths=list(TREE), now=NOW)
    assert m.sealed_at == "2026-10-12T21:22:22Z"
    assert [e.path for e in m.files] == sorted(TREE)
    assert m.source.files == len(TREE)
    assert m.by_path()["assets/brand/logo.png"].lines == 0
    d = m.to_dict()
    assert verify_manifest(d, m.root) == []
    again = Manifest.from_dict(json.loads(m.dumps()))
    assert again.root == m.root and again.files == m.files


def test_components_and_groups_add_up(tree):
    m = build(tree, SEED, paths=list(TREE), now=NOW)
    comps = m.components()
    assert sum(t.files for t in comps.values()) == m.source.files
    assert sum(t.lines for _, t in m.groups()) == m.source.lines
    labels = [g for g, _ in m.groups()]
    assert labels == [g for g, _ in GROUPS if g in labels]


@pytest.mark.parametrize("path,component", [
    ("quantumorb/sources/pumpfun.py", "bot · sources"),
    ("quantumorb/cli.py", "bot · core"),
    ("tests/fixtures/x.json", "bot · fixtures"),
    ("site/server/engine/live.js", "observatory · server"),
    ("site/package.json", "observatory · build & tests"),
    ("docker-compose.yml", "infrastructure"),
    ("pyproject.toml", "repository"),
])
def test_component_of(path, component):
    assert component_of(path) == component


def _good(tree):
    return build(tree, SEED, paths=list(TREE), now=NOW).to_dict()


@pytest.mark.parametrize("mutate,message", [
    (lambda d: d.update(scheme="qorb-seal-v0"), "scheme"),
    (lambda d: d.pop("root"), "root"),
    (lambda d: d.update(sealed_at="12.10.2026"), "sealed_at"),
    (lambda d: d["files"][0].update(commitment="xyz"), "commitment"),
    (lambda d: d["files"][0].update(path="../escape.py"), "inside"),
    (lambda d: d["files"][0].update(path="/etc/passwd"), "inside"),
    (lambda d: d["files"][0].update(lines=-1), "size"),
    (lambda d: d["files"][0].pop("bytes"), "malformed"),
    (lambda d: d["source"].update(lines=d["source"]["lines"] + 1), "totals"),
    (lambda d: d["files"][0].update(component="observatory · server"), "filed under"),
])
def test_strict_loading_refuses(tree, mutate, message):
    d = _good(tree)
    mutate(d)
    with pytest.raises(ManifestError, match=message):
        Manifest.from_dict(d)


def test_component_totals_are_checked(tree):
    d = _good(tree)
    first = next(iter(d["components"]))
    d["components"][first]["files"] += 1
    assert any("component totals" in p for p in verify_manifest(d, d["root"]))


def test_the_published_manifest_loads_strictly(repo):
    m = Manifest.load(repo / "sealed" / "manifest.json")
    assert m.root == (repo / "sealed" / "ROOT").read_text().strip()
    assert m.tree().root == m.root


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_tracked_files_follow_git(tree):
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    subprocess.run(["git", "-C", str(tree), "add", "quantumorb", "site"], check=True)
    (tree / "untracked.txt").write_text("not sealed\n")
    assert tracked_files(tree) == ["quantumorb/db.py", "quantumorb/pipeline/feed.py", "site/server/index.js"]
