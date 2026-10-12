"""The published contracts: each one cut from the sealed version of its file, and nothing else in it."""
import re

from vault.interfaces import audit, index_markdown, public_names_dts, public_names_pyi, read_contract, scan
from vault.manifest import Manifest

ROUTE = re.compile(r"https?://|/api/|frontend-api|swap-api|profile-api|getAccountInfo|\.env\b|[A-Z_]{4,}\s*=\s*\d")


def test_every_contract_matches_the_seal(repo):
    m = Manifest.load(repo / "sealed" / "manifest.json")
    contracts = scan(repo / "interfaces")
    assert len(contracts) >= 30
    assert audit(contracts, m, repo / "interfaces") == []
    assert {c.language for c in contracts} == {"python", "typescript"}


def test_the_index_is_current(repo):
    m = Manifest.load(repo / "sealed" / "manifest.json")
    want = "# Interface index\n\n" + index_markdown(scan(repo / "interfaces"), m)
    assert (repo / "interfaces" / "INDEX.md").read_text(encoding="utf-8") == want


def test_contracts_carry_no_routes_keys_or_thresholds(repo):
    for f in sorted((repo / "interfaces").rglob("*")):
        if f.suffix in (".pyi", ".ts"):
            body = f.read_text(encoding="utf-8")
            assert not ROUTE.search(body), f
            assert "def " not in body or "..." in body, f  # signatures, never bodies


def test_contracts_have_no_bodies(repo):
    import ast
    for f in sorted((repo / "interfaces").rglob("*.pyi")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # a docstring and `...`, nothing that could run
                assert all(isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant) for b in node.body), (
                    f, node.name)


def test_name_extraction():
    pyi = ("class A:\n    def run(self): ...\n    def _hidden(self): ...\n"
           "def f(): ...\nasync def g(): ...\ndef _h(): ...\n")
    assert public_names_pyi(pyi) == ["A", "A.run", "f", "g"]
    dts = ("export declare function start(): unknown;\nexport declare const LV: unknown; // read-only\n"
           "export interface Row {}\nconst hidden = 1;\n")
    assert public_names_dts(dts) == ["start", "LV", "Row"]


def test_a_stale_or_foreign_contract_fails(repo, tmp_path):
    m = Manifest.load(repo / "sealed" / "manifest.json")
    real = m.by_path()["quantumorb/db.py"].commitment
    good = tmp_path / "db.pyi"
    good.write_text(f"# QuantumOrb · interface contract · quantumorb/db.py\n"
                    f"# implementation: sealed · commitment {real[:16]}…{real[-8:]}\ndef open_db(): ...\n")
    stale = tmp_path / "old.pyi"
    stale.write_text("# QuantumOrb · interface contract · quantumorb/cli.py\n"
                     f"# implementation: sealed · commitment {'0' * 16}…{'0' * 8}\ndef main(): ...\n")
    foreign = tmp_path / "x.pyi"
    foreign.write_text("# QuantumOrb · interface contract · quantumorb/nowhere.py\n"
                       f"# implementation: sealed · commitment {'0' * 16}…{'0' * 8}\ndef x(): ...\n")
    (tmp_path / "bare.pyi").write_text("def y(): ...\n")
    contracts = [read_contract(f, tmp_path) for f in (good, stale, foreign)]
    problems = audit(contracts, m, tmp_path)
    assert any("old.pyi: cut from a different version" in p for p in problems)
    assert any("x.pyi: names quantumorb/nowhere.py" in p for p in problems)
    assert any("bare.pyi: no contract header" in p for p in problems)
    assert not any(p.startswith("db.pyi") for p in problems)
