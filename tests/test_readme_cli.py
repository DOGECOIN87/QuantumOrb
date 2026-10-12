"""The front page and the command line: what the README says is what the seal says."""
import json
import re
import subprocess
import sys

import pytest

from vault import cli
from vault.ledger import LEDGER, Ledger
from vault.manifest import Manifest
from vault.report import check, components_block, render_blocks, seal_block, sync


def test_readme_blocks_are_current(repo):
    m = Manifest.load(repo / "sealed" / "manifest.json")
    blocks = render_blocks(m, Ledger.load(repo / LEDGER))
    assert set(blocks) == {"seal", "components", "ledger"}
    assert check((repo / "README.md").read_text(encoding="utf-8"), blocks) == []


def test_sync_rewrites_only_inside_markers(repo):
    m = Manifest.load(repo / "sealed" / "manifest.json")
    text = "intro\n<!-- vault:seal -->\nold\n<!-- /vault:seal -->\noutro\n"
    out = sync(text, {"seal": seal_block(m)})
    assert out.startswith("intro\n") and out.endswith("\noutro\n") and m.root in out
    assert check(out, {"seal": seal_block(m)}) == []
    assert check(text, {"seal": seal_block(m)})
    with pytest.raises(KeyError):
        sync(text, {"components": components_block(m)})


def test_every_picture_is_there_and_captioned(repo):
    readme = (repo / "README.md").read_text(encoding="utf-8")
    pictures = re.findall(r'<img src="(docs/screens/[^"]+)"', readme)
    assert len(pictures) >= 10
    for p in pictures:
        assert (repo / p).is_file(), p
        tail = readme.split(p, 1)[1][:700]
        assert "<sub>" in tail, f"{p} has no caption"
    shipped = {f"docs/screens/{f.name}" for f in (repo / "docs" / "screens").glob("*.png")}
    assert shipped == set(pictures), "every screen shipped is shown, and only those"


def test_no_links_to_the_product_before_it_opens(repo):
    for f in [repo / "README.md", repo / "SEAL.md", repo / "CHANGELOG.md", *sorted((repo / "docs").glob("*.md"))]:
        text = f.read_text(encoding="utf-8").lower()
        assert "t.me/" not in text, f
        allowed = r"(?!github\.com|img\.shields\.io|json-schema\.org)"
        assert not re.search(r"https?://" + allowed + r"[a-z0-9.-]+\.[a-z]{2,}", text), f


def run(capsys, *argv):
    code = cli.main(["--repo", str(cli.HERE), *argv])
    return code, capsys.readouterr().out


def test_cli_checks(capsys):
    for argv in (["verify"], ["ledger", "--verify"], ["interfaces", "--check-index"], ["report", "--check"]):
        code, out = run(capsys, *argv)
        assert code == 0, (argv, out)
        assert out.startswith("ok"), out


def test_cli_status_and_timeline(capsys):
    code, out = run(capsys, "status")
    assert code == 0 and "qorb-seal-v1" in out and "contracts" in out
    code, out = run(capsys, "ledger")
    assert code == 0 and out.startswith("#1")


def test_cli_proof_round_trip(capsys, tmp_path, repo):
    code, out = run(capsys, "proof", "quantumorb/db.py")
    assert code == 0
    proof = tmp_path / "p.json"
    proof.write_text(out)
    code, out = run(capsys, "check-proof", str(proof))
    assert code == 0 and "quantumorb/db.py" in out
    d = json.loads(proof.read_text())
    d["proof"][0][1] = "0" * 64
    proof.write_text(json.dumps(d))
    code, out = run(capsys, "check-proof", str(proof))
    assert code == 1 and "does not fold back" in out
    code, out = run(capsys, "proof", "quantumorb/db.py", "--entry", "1")
    assert code == 0 and json.loads(out)["root"] == Ledger.load(repo / LEDGER).entries[0].root


def test_cli_diff(capsys, repo):
    code, out = run(capsys, "diff")
    assert code == 0
    if len(Ledger.load(repo / LEDGER).entries) > 1:
        assert "changed" in out and "unchanged" in out


def test_tools_seal_is_still_the_entry_point(repo):
    r = subprocess.run([sys.executable, str(repo / "tools" / "seal.py"), "verify"], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.startswith("ok"), r.stdout + r.stderr
    r = subprocess.run([sys.executable, "-m", "vault", "--version"], capture_output=True, text=True, cwd=repo)
    assert r.returncode == 0 and "qorb-seal-v1" in r.stdout
