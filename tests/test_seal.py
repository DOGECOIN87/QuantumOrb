"""The seal's own tests: the scheme is fixed, a proof folds back to the root, and nothing can be
changed - not a file, not a commitment, not a total - without the check saying so."""
import hashlib
import hmac
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "tools"))

import seal  # noqa: E402

SEED = bytes(range(32))


def test_scheme_is_fixed():
    # a known vector: if this changes, every published commitment would stop opening
    salt = seal.salt_for(SEED, "a/b.py")
    assert salt == hmac.new(SEED, b"qorb-seal-v1/salt/a/b.py", hashlib.sha256).digest()
    c = seal.commitment(salt, b"print('hi')\n")
    assert c == hmac.new(salt, b"print('hi')\n", hashlib.sha256).hexdigest()
    assert seal.leaf("a/b.py", c) == hashlib.sha256(b"\x00a/b.py\x00" + bytes.fromhex(c)).digest()


def test_salts_differ_per_file_and_hide_short_files():
    a, b = seal.salt_for(SEED, "x.py"), seal.salt_for(SEED, "y.py")
    assert a != b
    # the same five-line file under two paths gives unrelated commitments
    body = b"import sys\n"
    assert seal.commitment(a, body) != seal.commitment(b, body)


def test_every_proof_folds_back_to_the_root():
    for n in (1, 2, 3, 5, 8, 13, 155):
        leaves = [hashlib.sha256(str(i).encode()).digest() for i in range(n)]
        root = seal.merkle_root(leaves)
        for i in range(n):
            assert seal.check_proof(leaves[i].hex(), seal.merkle_proof(leaves, i), root), (n, i)
        if n > 1:
            assert not seal.check_proof(leaves[0].hex(), seal.merkle_proof(leaves, 1), root)


def _manifest(files: dict[str, bytes]) -> dict:
    rows = []
    for p in sorted(files):
        rows.append({"path": p, "component": seal.component_of(p), "lines": seal._lines(files[p]),
                     "bytes": len(files[p]), "commitment": seal.commitment(seal.salt_for(SEED, p), files[p])})
    return {"scheme": seal.SCHEME, "source": {"files": len(rows), "lines": sum(r["lines"] for r in rows),
            "bytes": sum(r["bytes"] for r in rows)}, "files": rows,
            "root": seal.merkle_root([seal.leaf(r["path"], r["commitment"]) for r in rows])}


def test_any_change_is_caught():
    files = {"quantumorb/a.py": b"x = 1\n", "site/server/b.js": b"module.exports={}\n", "docs/c.md": b"# c\n"}
    m = _manifest(files)
    assert seal.verify_manifest(m, m["root"]) == []
    bad = json.loads(json.dumps(m))
    bad["files"][1]["commitment"] = "0" * 64
    assert any("root" in p for p in seal.verify_manifest(bad, m["root"]))
    bad = json.loads(json.dumps(m))
    bad["source"]["lines"] += 1
    assert any("totals" in p for p in seal.verify_manifest(bad, m["root"]))
    assert any("ROOT" in p for p in seal.verify_manifest(m, "f" * 64))
    bad = json.loads(json.dumps(m))
    bad["files"].reverse()
    assert any("sorted" in p for p in seal.verify_manifest(bad, m["root"]))


def test_the_published_seal_is_consistent():
    r = subprocess.run([sys.executable, str(HERE / "tools" / "seal.py"), "verify"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    m = json.loads((HERE / "sealed" / "manifest.json").read_text())
    assert (HERE / "sealed" / "ROOT").read_text().strip() == m["root"]


def test_every_interface_names_a_sealed_commitment():
    m = json.loads((HERE / "sealed" / "manifest.json").read_text())
    commits = {f["path"]: f["commitment"] for f in m["files"]}
    stubs = list((HERE / "interfaces").rglob("*.pyi")) + list((HERE / "interfaces").rglob("*.d.ts"))
    assert stubs
    for s in stubs:
        head = s.read_text(encoding="utf-8")
        src = next(line.split("· ")[-1] for line in head.splitlines() if "interface contract" in line)
        c = commits[src.strip()]
        assert f"{c[:16]}…{c[-8:]}" in head, s
        if s.suffix == ".pyi":
            compile(head, str(s), "exec")


def test_the_readme_quotes_the_seal_it_ships_with():
    m = json.loads((HERE / "sealed" / "manifest.json").read_text())
    readme = (HERE / "README.md").read_text(encoding="utf-8")
    assert m["root"] in readme
    assert f"files     {m['source']['files']}" in readme
    assert f"lines     {m['source']['lines']:,}" in readme
