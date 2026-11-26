"""Schema v6: released confidential records.

A confidential submission is verified and rendered in the private caller
repository PalomarRegistry/PalomarConfidentialRuns, and its record carries a
`confidential` block exactly when that is so. Its submitted source may be
restored into a standalone PalomarArchive repository from Palomar's private
snapshot rather than forked, when the upstream was still private at release;
the v5 preservation shape already admits that, so v6 does not change it.

The block carries one certificate digest, of the full certificate. A digest of
the anonymous certificate would let a referee holding it find this record and
so unblind a double-blind review; it is refused.
"""

from __future__ import annotations

import copy
import json
import pathlib
import sys

import jsonschema
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import validate  # noqa: E402
from entry_validation import entry_consistency_errors  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "entry-v6-confidential.json"
SUBMISSION = "PalomarRegistry/PalomarSubmission"
RUNS = "PalomarRegistry/PalomarConfidentialRuns"


def _schema(version: int) -> dict:
    return json.loads((ROOT / f"schema-v{version}.json").read_text(encoding="utf-8"))


def _validator() -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(_schema(6))


def _schema_errors(record: dict) -> list[str]:
    return [error.message for error in _validator().iter_errors(record)]


def test_v6_is_v5_with_the_confidential_additions_and_nothing_else():
    expected = _schema(5)
    actual = _schema(6)
    expected["$id"] = "https://data.palomar-registry.org/schema-v6.json"
    expected["properties"]["schema_version"] = {"const": 6}
    verification = expected["properties"]["verification"]["properties"]
    render = expected["properties"]["challenge_render"]["properties"]
    added = actual["properties"]
    verification["repository"] = {"enum": [SUBMISSION, RUNS]}
    verification["workflow_path"] = {
        "enum": [".github/workflows/submission.yml", ".github/workflows/verify.yml"]
    }
    verification["called_workflow_commit"] = added["verification"]["properties"]["called_workflow_commit"]
    render["repository"] = {"enum": [SUBMISSION, RUNS]}
    render["workflow_path"] = {
        "enum": [".github/workflows/render-challenge.yml", ".github/workflows/render.yml"]
    }
    render["called_workflow_commit"] = added["challenge_render"]["properties"]["called_workflow_commit"]
    expected["properties"]["confidential"] = added["confidential"]
    expected["allOf"].append(actual["allOf"][-1])
    assert actual == expected
    # Nothing new is required of an ordinary record.
    for path in ("verification", "challenge_render"):
        assert actual["properties"][path]["required"] == _schema(5)["properties"][path]["required"]
    assert "confidential" not in actual["required"]
    block = actual["properties"]["confidential"]
    assert block["additionalProperties"] is False
    assert block["properties"]["certificate_sha256"]["additionalProperties"] is False
    assert set(block["properties"]) == {"held_at", "run_visibility", "certificate_sha256"}
    assert set(block["properties"]["certificate_sha256"]["properties"]) == {"full"}
    assert actual["properties"]["preservation"] == _schema(5)["properties"]["preservation"]


# The published record shape, as a committed fixture for the site and reviewer.


def synthetic_confidential_record() -> dict:
    """A fully synthetic released confidential record (no real submission data)."""
    identifier = "PALOMAR-2026-11-26-000001"
    version = 1
    source_repository = "example-org/confidential-fixture"
    source_commit = "1" * 40
    mathlib_commit = "2" * 40
    run_id = 41000000001
    pipeline_commit = "a" * 40
    tag = f"refs/tags/palomar/{identifier}-v{version}"
    evidence_tree = "e" * 64
    render_tree = "f" * 64
    return {
        "schema_version": 6,
        "id": identifier,
        "first_registered_on": "2026-11-26",
        "registered_at": "2026-11-26T09:00:00Z",
        "version": version,
        "status": "registered",
        "title": "Synthetic confidential fixture: every natural number is at most its successor",
        "abstract": "A synthetic record used to test confidential-mode tooling. It describes no real submission.\n",
        "authors": [{"name": "Example Author"}],
        "classification": {"arxiv": ["math.LO"], "msc2020": ["03B35"]},
        "provenance": {
            "mathematical_sources": [
                {
                    "authors": [{"name": "Example Author"}],
                    "identifier": "https://example.org/synthetic-confidential-fixture",
                    "relationship": "other",
                    "title": "Synthetic confidential fixture",
                }
            ],
            "related_formalizations": [],
            "repository_role": "substantive-development",
            "responsible_maintainers": [{"name": "Example Maintainer"}],
            "result_origin": "original",
        },
        "source": {
            "repository": source_repository,
            "repository_url": f"https://github.com/{source_repository}",
            "commit": source_commit,
            "tree_url": f"https://github.com/{source_repository}/tree/{source_commit}",
            "license": {
                "path": "LICENSE",
                "sha256": "3" * 64,
                "declared_identifier": "Apache-2.0",
                "detected_identifier": "Apache-2.0",
            },
        },
        "preservation": {
            "archive_owner": "PalomarArchive",
            "archived_at": "2026-11-26T08:55:00Z",
            "receipt_sha256": "4" * 64,
            "repositories": [
                {
                    "source_repository": source_repository,
                    "commit": source_commit,
                    "fork_repository": "PalomarArchive/example-org-confidential-fixture",
                    "ref": f"{tag}/{source_commit}",
                },
                {
                    "source_repository": "leanprover-community/mathlib4",
                    "commit": mathlib_commit,
                    "fork_repository": "PalomarArchive/mathlib4",
                    "ref": f"{tag}/{mathlib_commit}",
                },
            ],
        },
        "formalization": {
            "challenge_path": "Challenge.lean",
            "solution_path": "Solution.lean",
            "comparator_config_path": "comparator.json",
            "formalization_metadata_path": "formalization.yaml",
            "lakefile_path": "lakefile.toml",
            "lean_toolchain": "leanprover/lean4:v4.31.0",
            "definition_names": [],
            "theorem_names": ["SyntheticFixture.le_succ"],
            "permitted_axioms": ["propext", "Quot.sound", "Classical.choice"],
            "project_dependencies": [
                {
                    "name": "mathlib",
                    "repository": "leanprover-community/mathlib4",
                    "revision": mathlib_commit,
                }
            ],
        },
        "verification": {
            "repository": RUNS,
            "workflow_path": ".github/workflows/verify.yml",
            "called_workflow_commit": pipeline_commit,
            "run_id": run_id,
            "workflow_url": f"https://github.com/{RUNS}/actions/runs/{run_id}",
            "workflow_commit": "5" * 40,
            "workflow_run_attempt": 1,
            "verified_at": "2026-07-20T08:00:00Z",
            "evidence_path": f"evidence/{identifier}-v{version}/{evidence_tree}/",
            "evidence_tree_sha256": evidence_tree,
            "mechanical_report_sha256": "6" * 64,
            "challenge_sha256": "7" * 64,
            "solution_sha256": "8" * 64,
            "toolchain_commit": "9" * 40,
            "tool_digests": {
                name: "0" * 63 + str(position)
                for position, name in enumerate(
                    ("lake", "lean", "leanexport", "leanchecker", "nanoda_bin", "con-ron", "bwrap")
                )
            },
            "kernels": [
                {"name": "nanoda", "argv": ["/toolchain/bin/nanoda_bin"]},
                {"name": "con-ron", "argv": ["/toolchain/bin/con-ron"]},
            ],
            "protected_config_sha256": "b" * 64,
            "bwrap_source_tag": "v0.12.0",
        },
        "challenge_render": {
            "repository": RUNS,
            "workflow_path": ".github/workflows/render.yml",
            "called_workflow_commit": pipeline_commit,
            "format": "verso-html",
            "artifact_path": f"renders/{identifier}-v{version}/{render_tree}/",
            "entrypoint": "Challenge/index.html",
            "artifact_tree_sha256": render_tree,
            "verso_commit": "c" * 40,
            "renderer_commit": "d" * 40,
            "rendered_at": "2026-07-20T09:00:00Z",
            "bwrap_source_tag": "v0.12.0",
        },
        "review": {
            "policy_commit": "e" * 40,
            "reviewed_at": "2026-07-20T12:00:00Z",
            "reviewer_models": ["claude:default"],
            "outcome": "neutral",
            "warnings": [],
            "report": {"sha256": "f" * 64},
        },
        "trust": {
            "challenge_bytes": 120,
            "challenge_dependencies": [
                {"provenance": "allowlisted", "repository": "leanprover-community/mathlib4"}
            ],
            "challenge_imports": ["Mathlib"],
            "challenge_lines": 6,
            "level": "high",
            "reasons": [],
        },
        "submission": {
            "submission_id": "c0nf1d3nt1a1",
            "authorization": {"relationship": "maintainer"},
        },
        "confidential": {
            "held_at": "2026-07-21T10:00:00Z",
            "run_visibility": "private",
            "certificate_sha256": {"full": "1a" * 32},
        },
    }


def _encode(record: dict) -> str:
    return json.dumps(record, indent=2, ensure_ascii=False) + "\n"


def test_the_committed_confidential_fixture_is_current():
    assert FIXTURE.read_text(encoding="utf-8") == _encode(synthetic_confidential_record()), (
        "regenerate with: python tests/test_confidential_schema.py"
    )


def test_the_committed_confidential_fixture_satisfies_v6_and_its_invariants():
    record = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert _schema_errors(record) == []
    assert entry_consistency_errors("fixture", record) == []


def test_a_public_v6_record_is_a_v5_record_with_the_new_version():
    record = synthetic_confidential_record()
    record.pop("confidential")
    record["verification"].update({
        "repository": SUBMISSION,
        "workflow_path": ".github/workflows/submission.yml",
        "workflow_url": f"https://github.com/{SUBMISSION}/actions/runs/{record['verification']['run_id']}",
    })
    record["verification"].pop("called_workflow_commit")
    for field in ("repository", "workflow_path", "called_workflow_commit"):
        record["challenge_render"].pop(field)
    assert _schema_errors(record) == []
    assert entry_consistency_errors("public", record) == []
    as_v5 = copy.deepcopy(record)
    as_v5["schema_version"] = 5
    assert jsonschema.Draft202012Validator(_schema(5)).is_valid(as_v5)


def _public(record: dict) -> dict:
    record = copy.deepcopy(record)
    record["verification"].update({
        "repository": SUBMISSION,
        "workflow_path": ".github/workflows/submission.yml",
        "workflow_url": f"https://github.com/{SUBMISSION}/actions/runs/{record['verification']['run_id']}",
    })
    record["verification"].pop("called_workflow_commit")
    for field in ("repository", "workflow_path", "called_workflow_commit"):
        record["challenge_render"].pop(field)
    return record


def _mutations() -> dict[str, dict]:
    base = synthetic_confidential_record()
    cases: dict[str, dict] = {}

    block_on_public_run = _public(base)
    cases["a confidential block on a PalomarSubmission run"] = block_on_public_run

    missing = copy.deepcopy(base)
    missing.pop("confidential")
    cases["a runs-repository record without the block"] = missing

    expiry = copy.deepcopy(base)
    expiry["confidential"]["expires_on"] = "2028-07-21"
    cases["embargo terms in the block"] = expiry

    certificate = copy.deepcopy(base)
    certificate["confidential"]["certificate_sha256"]["extra"] = "0" * 64
    cases["an extra certificate digest"] = certificate

    anonymous = copy.deepcopy(base)
    anonymous["confidential"]["certificate_sha256"]["anonymous"] = "2b" * 32
    cases["an anonymous-certificate digest"] = anonymous

    no_full = copy.deepcopy(base)
    no_full["confidential"]["certificate_sha256"].pop("full")
    cases["a missing full-certificate digest"] = no_full

    visibility = copy.deepcopy(base)
    visibility["confidential"]["run_visibility"] = "public"
    cases["a public run visibility"] = visibility

    wrong_workflow = copy.deepcopy(base)
    wrong_workflow["verification"]["workflow_path"] = ".github/workflows/submission.yml"
    cases["the runs repository with submission.yml"] = wrong_workflow

    public_verify = _public(base)
    public_verify.pop("confidential")
    public_verify["verification"]["workflow_path"] = ".github/workflows/verify.yml"
    cases["PalomarSubmission with verify.yml"] = public_verify

    no_called = copy.deepcopy(base)
    no_called["verification"].pop("called_workflow_commit")
    cases["a confidential run without its called workflow commit"] = no_called

    public_called = _public(base)
    public_called.pop("confidential")
    public_called["verification"]["called_workflow_commit"] = "a" * 40
    cases["a public run with a called workflow commit"] = public_called

    public_render = copy.deepcopy(base)
    public_render["challenge_render"]["repository"] = SUBMISSION
    cases["a confidential record rendered in PalomarSubmission"] = public_render

    no_render_origin = copy.deepcopy(base)
    no_render_origin["challenge_render"].pop("repository")
    cases["a confidential record without its render origin"] = no_render_origin

    confidential_render = _public(base)
    confidential_render.pop("confidential")
    confidential_render["challenge_render"].update({
        "repository": RUNS,
        "workflow_path": ".github/workflows/render.yml",
    })
    cases["a public record rendered in the runs repository"] = confidential_render

    unknown_repository = copy.deepcopy(base)
    unknown_repository["verification"]["repository"] = "PalomarRegistry/Elsewhere"
    cases["an unknown verifier repository"] = unknown_repository
    return cases


MUTATIONS = _mutations()


@pytest.mark.parametrize("case", sorted(MUTATIONS))
def test_v6_refuses(case):
    assert _schema_errors(MUTATIONS[case]), case


def test_only_v6_admits_the_runs_repository():
    for version in (3, 4, 5):
        assert _schema(version)["properties"]["verification"]["properties"]["repository"] == {
            "const": SUBMISSION
        }
        assert "confidential" not in _schema(version)["properties"]
    record = synthetic_confidential_record()
    record["schema_version"] = 5
    errors = entry_consistency_errors("v5", record)
    assert any("verification.repository" in error for error in errors), errors


def test_the_consistency_checks_bind_the_block_to_the_runs_repository():
    record = synthetic_confidential_record()
    record.pop("confidential")
    errors = entry_consistency_errors("missing", record)
    assert any("confidential: must be present exactly" in error for error in errors), errors
    record = _public(synthetic_confidential_record())
    errors = entry_consistency_errors("extra", record)
    assert any("confidential: must be present exactly" in error for error in errors), errors


def test_the_run_url_is_derived_from_the_runs_repository():
    record = synthetic_confidential_record()
    record["verification"]["workflow_url"] = (
        f"https://github.com/{SUBMISSION}/actions/runs/{record['verification']['run_id']}"
    )
    errors = entry_consistency_errors("url", record)
    assert any("verification.workflow_url" in error for error in errors), errors


# The whole validator, against a database with evidence bundles.


@pytest.mark.parametrize("version", [3, 4, 5, 6])
def test_every_schema_still_validates_alongside_v6(repo, version):
    identifier = repo.next_identifier()
    data = repo.entry_data(identifier, 1)
    if version == 4:
        data["schema_version"] = 4
    elif version == 5:
        data = repo.toolchain_provenance(data)
    elif version == 6:
        data = repo.toolchain_provenance(data)
        data["schema_version"] = 6
    repo.install_entry(data)
    assert validate.validate(repo.path) == []


def test_a_confidential_record_and_its_evidence_validate(repo):
    identifier = repo.next_identifier()
    repo.install_entry(repo.confidential(repo.entry_data(identifier, 1)))
    assert validate.validate(repo.path) == []


def test_the_run_provenance_must_name_the_runs_repository(repo):
    identifier = repo.next_identifier()
    data = repo.confidential(repo.entry_data(identifier, 1))
    repo.install_entry(data)
    stored = repo.read_json(f"entries/{identifier}-v1.json")
    run_path = repo.path / stored["verification"]["evidence_path"] / "workflow-run.json"
    provenance = json.loads(run_path.read_text())
    provenance["workflow_path"] = ".github/workflows/submission.yml"
    run_path.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")
    errors = validate.validate(repo.path)
    assert any("run provenance disagrees on workflow_path" in error for error in errors), errors
    provenance["workflow_path"] = ".github/workflows/verify.yml"
    provenance["repository"] = SUBMISSION
    run_path.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")
    errors = validate.validate(repo.path)
    assert any("run provenance disagrees on repository" in error for error in errors), errors


def test_the_report_must_name_the_caller_and_the_called_pipeline(repo):
    identifier = repo.next_identifier()
    repo.install_entry(repo.confidential(repo.entry_data(identifier, 1)))
    stored = repo.read_json(f"entries/{identifier}-v1.json")
    stored["verification"]["called_workflow_commit"] = "b" * 40
    repo.write_json(f"entries/{identifier}-v1.json", stored)
    errors = validate.validate(repo.path)
    assert any("disagrees on pipeline_commit" in error for error in errors), errors


def test_the_hold_precedes_the_release():
    record = synthetic_confidential_record()
    record["confidential"]["held_at"] = "2026-11-27T00:00:00Z"
    errors = entry_consistency_errors("late", record)
    assert any("held_at" in error for error in errors), errors


def test_a_database_without_the_v6_contract_still_validates_older_records(repo):
    (repo.path / "schema-v6.json").unlink()
    repo.install_entry(repo.toolchain_provenance(repo.entry_data(repo.next_identifier(), 1)))
    assert validate.validate(repo.path) == []


def test_a_v6_record_needs_the_v6_contract_to_be_published(repo):
    (repo.path / "schema-v6.json").unlink()
    repo.install_entry(repo.confidential(repo.entry_data(repo.next_identifier(), 1)))
    errors = validate.validate(repo.path)
    assert any("schema-v6.json is not published" in error for error in errors), errors


def test_the_v6_contract_may_be_added_after_launch_but_never_changed(repo):
    import check_append_only

    (repo.path / "schema-v6.json").unlink()
    repo.git("add", "-A")
    base = repo.commit("a launched database without the v6 contract")
    (repo.path / "schema-v6.json").write_bytes((ROOT / "schema-v6.json").read_bytes())
    repo.git("add", "-A")
    added = repo.commit("publish the v6 contract")
    assert check_append_only.check(repo.path, base, added) == []
    schema = json.loads((repo.path / "schema-v6.json").read_text())
    schema["title"] = "changed"
    (repo.path / "schema-v6.json").write_text(json.dumps(schema, indent=2) + "\n")
    repo.git("add", "-A")
    changed = repo.commit("tamper with the v6 contract")
    assert any("schema-v6.json" in error for error in check_append_only.check(repo.path, added, changed))


def test_a_confidential_record_publishes(repo, served):
    identifier = repo.next_identifier()
    repo.install_entry(repo.confidential(repo.entry_data(identifier, 1)))
    repo.commit("a released confidential record")
    site = served.publish()
    assert (site / "schema-v6.json").is_file()
    published = json.loads((site / f"entries/{identifier}-v1.json").read_text())
    assert published["confidential"]["run_visibility"] == "private"


if __name__ == "__main__":
    FIXTURE.write_text(_encode(synthetic_confidential_record()), encoding="utf-8")
