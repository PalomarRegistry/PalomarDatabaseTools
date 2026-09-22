"""Schema v5: records verified by the Lean toolchain's own `lake comparator`."""

from __future__ import annotations

import copy
import json
import pathlib

import pytest

import validate

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOL_COMMITS = ("comparator_commit", "lean4export_commit", "landrun_commit", "nanoda_commit")
PROVENANCE = ("toolchain_commit", "tool_digests", "kernels", "protected_config_sha256", "bwrap_source_tag")


def _schema(version: int) -> dict:
    return json.loads((ROOT / f"schema-v{version}.json").read_text(encoding="utf-8"))


def test_v5_is_v4_with_toolchain_provenance_and_nothing_else():
    expected = _schema(4)
    expected["$id"] = "https://data.palomar-registry.org/schema-v5.json"
    expected["properties"]["schema_version"] = {"const": 5}
    verification = expected["properties"]["verification"]
    for field in TOOL_COMMITS:
        verification["properties"].pop(field)
        verification["required"].remove(field)
    actual = _schema(5)
    for field in PROVENANCE:
        verification["properties"][field] = actual["properties"]["verification"]["properties"][field]
    verification["required"] = sorted({*verification["required"], *PROVENANCE})
    render = expected["properties"]["challenge_render"]
    render["properties"].pop("landrun_commit")
    render["required"].remove("landrun_commit")
    render["properties"]["bwrap_source_tag"] = {"$ref": "#/$defs/release_tag"}
    render["required"].append("bwrap_source_tag")
    expected["$defs"]["release_tag"] = actual["$defs"]["release_tag"]
    assert actual == expected
    assert actual["properties"]["verification"]["properties"]["toolchain_commit"] == {"$ref": "#/$defs/sha"}
    digests = actual["properties"]["verification"]["properties"]["tool_digests"]
    assert digests["additionalProperties"] is False
    assert digests["required"] == ["lake", "lean", "leanexport", "leanchecker", "nanoda_bin", "con-ron", "bwrap"]
    kernels = actual["properties"]["verification"]["properties"]["kernels"]
    assert kernels["minItems"] == 1 and kernels["items"]["required"] == ["name", "argv"]


def _errors(repo) -> list[str]:
    return [error for error in validate.validate(repo.path) if "schema-v" in error or "verification" in error]


def test_a_v5_record_and_its_schema_2_report_validate(repo):
    identifier = repo.next_identifier()
    data = repo.toolchain_provenance(repo.entry_data(identifier, 1))
    repo.install_entry(data)
    assert _errors(repo) == []


def test_a_v5_record_may_not_carry_the_retired_tool_commits(repo):
    identifier = repo.next_identifier()
    data = repo.toolchain_provenance(repo.entry_data(identifier, 1))
    data["verification"]["landrun_commit"] = "8" * 40
    repo.install_entry(data)
    assert any("landrun_commit" in error for error in _errors(repo))


def test_a_v5_record_must_copy_its_provenance_from_the_report(repo):
    identifier = repo.next_identifier()
    data = repo.toolchain_provenance(repo.entry_data(identifier, 1))
    repo.install_entry(data)
    data["verification"]["toolchain_commit"] = "7" * 40
    repo.write_json(f"entries/{identifier}-v1.json", data)
    errors = _errors(repo)
    assert any("toolchain_commit" in error for error in errors), errors


@pytest.mark.parametrize("version", [3, 4, 5])
def test_every_report_contract_is_the_one_its_entry_schema_expects(repo, version):
    identifier = repo.next_identifier()
    data = repo.entry_data(identifier, 1)
    if version == 5:
        data = repo.toolchain_provenance(data)
    elif version == 4:
        data["schema_version"] = 4
    repo.install_entry(data)
    entry_path = repo.path / f"entries/{identifier}-v1.json"
    stored = json.loads(entry_path.read_text())
    bundle = repo.path / stored["verification"]["evidence_path"] / "mechanical-report.json"
    report = json.loads(bundle.read_text())
    assert report["schema_version"] == (2 if version == 5 else 1)
    wrong = copy.deepcopy(report)
    wrong["schema_version"] = 1 if version == 5 else 2
    bundle.write_text(json.dumps(wrong, indent=2, sort_keys=True) + "\n")
    assert any("mechanical report schema" in error for error in _errors(repo))


def test_a_database_without_the_v5_contract_still_validates_older_records(repo):
    (repo.path / "schema-v5.json").unlink()
    identifier = repo.next_identifier()
    repo.install_entry(repo.entry_data(identifier, 1))
    assert validate.validate(repo.path) == []


def test_a_v5_record_needs_the_v5_contract_to_be_published(repo):
    (repo.path / "schema-v5.json").unlink()
    identifier = repo.next_identifier()
    repo.install_entry(repo.toolchain_provenance(repo.entry_data(identifier, 1)))
    errors = validate.validate(repo.path)
    assert any("schema-v5.json is not published" in error for error in errors), errors


def test_a_v5_record_validates_completely(repo):
    identifier = repo.next_identifier()
    repo.install_entry(repo.toolchain_provenance(repo.entry_data(identifier, 1)))
    assert validate.validate(repo.path) == []


@pytest.mark.parametrize("field", PROVENANCE)
def test_every_provenance_field_is_bound_to_the_report(repo, field):
    identifier = repo.next_identifier()
    data = repo.toolchain_provenance(repo.entry_data(identifier, 1))
    repo.install_entry(data)
    stored = json.loads((repo.path / f"entries/{identifier}-v1.json").read_text())
    changed = {
        "toolchain_commit": "7" * 40,
        "tool_digests": {**stored["verification"]["tool_digests"], "lake": "7" * 64},
        "kernels": [{"name": "nanoda", "argv": ["/elsewhere/nanoda_bin"]}],
        "protected_config_sha256": "7" * 64,
        "bwrap_source_tag": "v0.13.0",
    }[field]
    stored["verification"][field] = changed
    repo.write_json(f"entries/{identifier}-v1.json", stored)
    assert any(field in error for error in validate.validate(repo.path))


def test_the_v5_contract_may_be_added_after_launch_but_never_changed(repo):
    import check_append_only

    (repo.path / "schema-v5.json").unlink()
    repo.git("add", "-A")
    base = repo.commit("a launched database without the v5 contract")
    (repo.path / "schema-v5.json").write_bytes((ROOT / "schema-v5.json").read_bytes())
    repo.git("add", "-A")
    added = repo.commit("publish the v5 contract")
    assert check_append_only.check(repo.path, base, added) == []
    schema = json.loads((repo.path / "schema-v5.json").read_text())
    schema["title"] = "changed"
    (repo.path / "schema-v5.json").write_text(json.dumps(schema, indent=2) + "\n")
    repo.git("add", "-A")
    changed = repo.commit("tamper with the v5 contract")
    assert any("schema-v5.json" in error for error in check_append_only.check(repo.path, added, changed))
    (repo.path / "schema-v5.json").unlink()
    repo.git("add", "-A")
    deleted = repo.commit("delete the v5 contract")
    assert any("schema-v5.json" in error for error in check_append_only.check(repo.path, changed, deleted))
