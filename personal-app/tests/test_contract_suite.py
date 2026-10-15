"""The release gate uses the real offline pipeline and fails on contract drift."""

import json
import shutil
import socket

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from scripts.verify_contracts import ROOT, main, mismatch_paths, verify_contracts


def test_contract_gate_runs_with_network_forbidden_and_no_key(monkeypatch):
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)

    def no_network(*args, **kwargs):
        pytest.fail("offline contract verification attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    report = verify_contracts()
    assert report == {
        "status": "ok",
        "schemas_checked": 8,
        "idl_snapshots_checked": 2,
        "golden_vectors_checked": 3,
        "replay_passes": 2,
        "migration_consistent": True,
        "mismatches": [],
    }


@pytest.mark.parametrize("mutation", ["changed", "missing", "extra"])
def test_contract_cli_fails_with_fixture_and_path_but_no_values(tmp_path, capsys, mutation):
    golden_dir = tmp_path / "goldens"
    shutil.copytree(ROOT / "coding-pack/golden-tests", golden_dir)
    path = golden_dir / "create-event.expected.json"
    expected = json.loads(path.read_text())
    if mutation == "missing":
        del expected["mint"]
    elif mutation == "extra":
        expected["unexpected"] = "synthetic-private-value"
    else:
        expected["mint"] = "synthetic-private-value"
    path.write_text(json.dumps(expected))
    assert main(["--golden-dir", str(golden_dir)]) == 2
    output = capsys.readouterr().out
    report = json.loads(output)
    field = "unexpected" if mutation == "extra" else "mint"
    assert report["mismatches"] == [f"pump-create-event.json: $.{field}"]
    assert "synthetic-private-value" not in output


def test_nested_contract_mismatches_include_all_paths_and_exact_json_types():
    assert mismatch_paths(
        {"counts": [1, {"read": False}], "missing": None},
        {"counts": [True, {"read": True}, 2], "extra": None},
    ) == ["$.counts[0]", "$.counts[1].read", "$.counts[2]", "$.extra", "$.missing"]


@pytest.mark.parametrize("value", [None, "8a4ed9a1-ea8b-4b58-8acd-b7b6d018884a", "bad-uuid"])
def test_summary_reference_schema_enforces_uuid_format(value):
    schema = json.loads(
        (ROOT / "coding-pack/schemas/cartographer-evidence.schema.json").read_text()
    )
    validator = Draft202012Validator(
        schema["properties"]["summary_ref"], format_checker=FormatChecker()
    )
    if value == "bad-uuid":
        with pytest.raises(ValidationError):
            validator.validate(value)
    else:
        validator.validate(value)
