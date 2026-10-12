"""The published JSON follows its schema, and the standalone example agrees with the vault."""
import json
import subprocess
import sys

import pytest

from vault.ledger import Ledger
from vault.merkle import Proof

jsonschema = pytest.importorskip("jsonschema")


def schema(repo, name):
    return json.loads((repo / "schemas" / f"{name}.schema.json").read_text())


def test_schemas_are_valid_schemas(repo):
    for f in sorted((repo / "schemas").glob("*.schema.json")):
        jsonschema.Draft202012Validator.check_schema(json.loads(f.read_text()))


def test_manifests_follow_the_schema(repo):
    v = jsonschema.Draft202012Validator(schema(repo, "manifest"))
    for f in [repo / "sealed" / "manifest.json", *sorted((repo / "sealed" / "archive").glob("*.json"))]:
        errors = list(v.iter_errors(json.loads(f.read_text())))
        assert not errors, (f, errors[:1])


def test_the_ledger_follows_the_schema(repo):
    jsonschema.validate(json.loads((repo / "sealed" / "ledger.json").read_text()), schema(repo, "ledger"))


def test_example_proofs(repo):
    roots = {e.root for e in Ledger.load(repo / "sealed" / "ledger.json").entries}
    proofs = sorted((repo / "examples" / "proofs").glob("*.json"))
    assert proofs
    for f in proofs:
        d = json.loads(f.read_text())
        jsonschema.validate(d, schema(repo, "proof"))
        assert Proof.from_dict(d).verify()
        assert d["root"] in roots, f"{f.name}: a root this repository never published"
        r = subprocess.run([sys.executable, "-I", str(repo / "examples" / "verify_offline.py"), str(f)],
                           capture_output=True, text=True)
        assert r.returncode == 0 and r.stdout.startswith("ok"), r.stdout + r.stderr


def test_the_offline_example_refuses_a_bad_proof(repo, tmp_path):
    f = sorted((repo / "examples" / "proofs").glob("*.json"))[0]
    d = json.loads(f.read_text())
    d["root"] = "f" * 64
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(d))
    r = subprocess.run([sys.executable, "-I", str(repo / "examples" / "verify_offline.py"), str(bad)],
                       capture_output=True, text=True)
    assert r.returncode == 1 and "FAIL" in r.stdout
