"""Self-consistency checks for entries, schemas, and registration projections."""

from __future__ import annotations

import hashlib
import json
import pathlib
import shutil

import pytest

from entry_validation import (
    entry_schema_violations,
    load_entry_schema,
    preservation_errors,
)
from render_validation import RENDER_CSP_META
from validate import validate

ROOT = pathlib.Path(__file__).resolve().parents[1]


def readdress_render(db, data):
    """Recompute a deliberately modified bundle's honest content address."""
    old_bundle = db.path / data["challenge_render"]["artifact_path"]
    files = []
    for path in sorted(old_bundle.rglob("*")):
        relative = path.relative_to(old_bundle).as_posix()
        if not path.is_file() or relative == "artifact-manifest.json":
            continue
        content = path.read_bytes()
        files.append(
            {
                "path": relative,
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    tree_hash = hashlib.sha256(canonical).hexdigest()
    (old_bundle / "artifact-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifact_tree_sha256": tree_hash,
                "files": files,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    new_relative = f"renders/{data['id']}-v{data['version']}/{tree_hash}/"
    new_bundle = db.path / new_relative
    new_bundle.parent.mkdir(parents=True, exist_ok=True)
    old_bundle.rename(new_bundle)
    data["challenge_render"]["artifact_path"] = new_relative
    data["challenge_render"]["artifact_tree_sha256"] = tree_hash
    db.install_entry(data)


def test_the_real_database_is_valid():
    if not (ROOT / "takedowns.json").is_file():
        pytest.skip("public tooling checkout, not the canonical database")
    assert validate(ROOT) == []


def test_the_canonical_entry_fixture_matches_schema_and_preservation_contract():
    entry = json.loads((ROOT / "tests/fixtures/entry.json").read_text(encoding="utf-8"))
    validator, errors = load_entry_schema(ROOT)

    assert errors == []
    assert validator is not None
    assert entry_schema_violations(validator, entry) == []
    assert len(entry["preservation"]["repositories"]) == 17
    assert preservation_errors("tests/fixtures/entry.json", entry) == []


def test_a_fresh_database_is_valid(db):
    assert validate(db.path) == []


def test_adding_a_version_is_valid(db):
    db.add_entry("PALOMAR-2026-07-29-000001", 2)
    assert validate(db.path) == []


def test_later_version_may_change_substantive_fields(db):
    data = db.entry_data("PALOMAR-2026-07-29-000001", 2)
    data["title"] = "An entirely different result"
    data["abstract"] = "This version need not resemble version one."
    data["authors"] = [{"name": "A different author"}]
    data["source"]["commit"] = "a" * 40
    data["source"]["tree_url"] = (
        "https://github.com/kim-em/erdos-unit-distance-comparator/tree/" + "a" * 40
    )
    data["formalization"]["theorem_names"] = ["Completely.Different.theorem"]
    db.install_entry(data)
    assert validate(db.path) == []


def test_later_version_cannot_change_stable_registration_identity(db):
    data = db.entry_data("PALOMAR-2026-07-29-000001", 2)
    data["source"].update({
        "repository": "someone/another-project",
        "repository_url": "https://github.com/someone/another-project",
        "tree_url": "https://github.com/someone/another-project/tree/" + data["source"]["commit"],
    })
    db.install_entry(data)

    assert any(
        "stable registration identity changed" in error
        for error in validate(db.path)
    )


def test_filename_must_match_id_and_version(db):
    data = db.entry_data("PALOMAR-2026-07-29-000002", 1)
    db.install_entry(data)
    db.rename_entry(data, "entries/PALOMAR-2026-07-29-000002-v9.json")
    errors = validate(db.path)
    assert any("filename must be PALOMAR-2026-07-29-000002-v1.json" in error for error in errors)


def test_duplicate_version_under_another_filename_is_rejected(db):
    """Two files may not claim the same id and version; the filename rule forbids it."""
    data = db.entry_data("PALOMAR-2026-07-29-000001", 1)
    db.install_entry(data)
    db.copy_entry(data, "entries/PALOMAR-2026-07-29-000001-v1-copy.json")
    errors = validate(db.path)
    assert any("PALOMAR-2026-07-29-000001-v1-copy.json: filename must be" in error for error in errors)


def test_result_projection_drift_is_rejected(db):
    relative = "registrations/results/PALOMAR-2026-07-29-000001.json"
    result = db.read_json(relative)
    result["versions"][0]["title"] = "not the title in the entry file"
    db.write_json(relative, result)
    errors = validate(db.path)
    assert any("does not exactly match canonical entries" in error for error in errors)


def test_result_projection_missing_an_entry_is_rejected(db):
    db.add_entry("PALOMAR-2026-07-29-000002", 1)
    db.remove("registrations/results/PALOMAR-2026-07-29-000002.json")
    errors = validate(db.path)
    assert any("projection paths disagree" in error for error in errors)


def test_unsupported_result_projection_schema_version_is_rejected(db):
    relative = "registrations/results/PALOMAR-2026-07-29-000001.json"
    result = db.read_json(relative)
    result["schema_version"] = 99
    db.write_json(relative, result)
    errors = validate(db.path)
    assert any("does not exactly match canonical entries" in error for error in errors)


@pytest.mark.parametrize("version", [1, 2.0, 7, "2", True, None])
def test_entry_missing_or_wrong_schema_version_is_rejected_clearly(db, version):
    data = db.entry_data("PALOMAR-2026-07-29-000002", 1)
    if version is None:
        data.pop("schema_version")
    else:
        data["schema_version"] = version
    db.install_entry(data)
    errors = validate(db.path)
    assert any(
        "schema_version must equal 2" in error
        and "schema-v2.json is the sole current entry contract" in error
        for error in errors
    )


def test_an_alternate_entry_schema_document_is_rejected(db):
    shutil.copy(db.path / "schema-v2.json", db.path / "schema-v3.json")
    errors = validate(db.path)
    assert any(
        "schema-v3.json: unsupported entry schema document" in error
        for error in errors
    )


def test_an_alternate_entry_schema_symlink_is_rejected(db):
    (db.path / "schema-v3.json").symlink_to("schema-v2.json")
    errors = validate(db.path)
    assert any(
        "schema-v3.json: unsupported entry schema document" in error
        for error in errors
    )


def test_the_sole_entry_schema_must_exist(db):
    db.remove("schema-v2.json")
    errors = validate(db.path)
    assert any(
        "schema-v2.json: the sole entry schema is missing or symbolic" in error
        for error in errors
    )


def test_the_sole_entry_schema_must_not_be_a_symlink(db):
    db.remove("schema-v2.json")
    (db.path / "schema-v2.json").symlink_to("README.md")
    assert any(
        "schema-v2.json: the sole entry schema is missing or symbolic" in error
        for error in validate(db.path)
    )


def test_the_sole_entry_schema_must_not_be_executable(db):
    (db.path / "schema-v2.json").chmod(0o755)
    assert any(
        "schema-v2.json: the sole entry schema must be a non-executable ordinary file"
        in error
        for error in validate(db.path)
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_path", "../project"),
        ("challenge_path", "project/../Challenge.lean"),
        ("lakefile_path", "project/nested/lakefile.toml"),
    ],
)
def test_entry_schema_rejects_unsafe_or_inconsistent_project_paths(db, field, value):
    data = db.entry_data("PALOMAR-2026-07-29-000002", 1)
    data["source"].update(
        {
            "project_path": "project",
            "tree_url": data["source"]["tree_url"] + "/project",
        }
    )
    for name in ("challenge_path", "solution_path", "comparator_config_path"):
        data["formalization"][name] = f"project/{data['formalization'][name]}"
    data["formalization"]["lakefile_path"] = "project/lakefile.toml"
    if field == "project_path":
        data["source"][field] = value
    else:
        data["formalization"][field] = value
    db.install_entry(data)
    errors = validate(db.path)
    assert any(field in error for error in errors)


def test_tree_url_and_evidence_are_bound_to_recorded_paths(db):
    path = db.add_entry("PALOMAR-2026-07-29-000002", 1)
    data = db.read_json(path.relative_to(db.path).as_posix())
    data["source"]["tree_url"] += "/wrong"
    data["formalization"]["challenge_path"] = "Other.lean"
    db.write_json(path.relative_to(db.path).as_posix(), data)
    db.reindex()
    errors = validate(db.path)
    assert any("source.tree_url: must be" in error for error in errors)
    assert any(
        "mechanical report disagrees on formalization.challenge_path" in error
        for error in errors
    )


def test_the_mechanical_report_schema_is_pinned(db):
    path = db.add_entry("PALOMAR-2026-07-29-000002", 1)
    data = db.read_json(path.relative_to(db.path).as_posix())
    report_path = (
        db.path / data["verification"]["evidence_path"] / "mechanical-report.json"
    )
    report = json.loads(report_path.read_text())
    report["schema_version"] = 2
    report_path.write_text(json.dumps(report))
    assert any(
        "requires mechanical report schema 1" in error
        for error in validate(db.path)
    )


def test_entry_schema_rejects_unsafe_path_dependency(db):
    data = db.entry_data("PALOMAR-2026-07-29-000002", 1)
    data["formalization"]["project_dependencies"] = [
        {"name": "local", "path": "../outside"}
    ]
    db.install_entry(data)
    assert any("project_dependencies.0.path" in error for error in validate(db.path))


def test_verification_evidence_stays_bound_to_its_record(db):
    db.add_entry("PALOMAR-2026-07-29-000002", 1)
    assert validate(db.path) == []


def test_a_tampered_mechanical_report_is_rejected(db):
    path = db.add_entry("PALOMAR-2026-07-29-000002", 1)
    entry = db.read_json(path.relative_to(db.path).as_posix())
    report = db.path / entry["verification"]["evidence_path"] / "mechanical-report.json"
    report.write_text("{}\n", encoding="utf-8")
    errors = validate(db.path)
    assert any("mechanical_report_sha256 does not match" in error for error in errors)
    assert any("mechanical report disagrees" in error for error in errors)


def test_preservation_must_cover_the_complete_source_graph(db):
    path = db.add_entry("PALOMAR-2026-07-29-000002", 1)
    entry = db.read_json(path.relative_to(db.path).as_posix())
    entry["preservation"]["repositories"].pop()
    db.write_json(path.relative_to(db.path).as_posix(), entry)
    db.reindex()
    assert any(
        "must exactly cover source, Git dependencies" in error
        for error in validate(db.path)
    )


def test_preservation_refs_are_immutable_record_specific_tags(db):
    path = db.add_entry("PALOMAR-2026-07-29-000002", 1)
    entry = db.read_json(path.relative_to(db.path).as_posix())
    entry["preservation"]["repositories"][0]["ref"] = "refs/tags/moving"
    db.write_json(path.relative_to(db.path).as_posix(), entry)
    db.reindex()
    assert any("preservation.repositories.0.ref" in error for error in validate(db.path))


def test_source_archive_receipt_is_bound_to_the_entry(db):
    path = db.add_entry("PALOMAR-2026-07-29-000002", 1)
    entry = db.read_json(path.relative_to(db.path).as_posix())
    receipt = db.path / entry["verification"]["evidence_path"] / "source-archive.json"
    receipt.write_text("{}\n", encoding="utf-8")
    errors = validate(db.path)
    assert any("receipt_sha256 does not match" in error for error in errors)
    assert any("receipt does not match preservation metadata" in error for error in errors)


def test_run_provenance_drift_is_rejected(db):
    path = db.add_entry("PALOMAR-2026-07-29-000002", 1)
    entry = db.read_json(path.relative_to(db.path).as_posix())
    run_path = db.path / entry["verification"]["evidence_path"] / "workflow-run.json"
    run = json.loads(run_path.read_text())
    run["run_attempt"] = 2
    run_path.write_text(json.dumps(run, indent=2, sort_keys=True) + "\n")
    errors = validate(db.path)
    assert any("run provenance disagrees on run_attempt" in error for error in errors)


def test_unsuccessful_or_undated_run_jobs_are_rejected(db):
    path = db.add_entry("PALOMAR-2026-07-29-000002", 1)
    entry = db.read_json(path.relative_to(db.path).as_posix())
    run_path = db.path / entry["verification"]["evidence_path"] / "workflow-run.json"
    run = json.loads(run_path.read_text())
    run["created_at"] = None
    run["jobs"][0]["conclusion"] = "failure"
    run_path.write_text(json.dumps(run, indent=2, sort_keys=True) + "\n")
    errors = validate(db.path)
    assert any("invalid created_at" in error for error in errors)
    assert any("run job 0 is malformed" in error for error in errors)


def test_evidence_without_the_archived_review_is_rejected(db):
    """The review the decision rests on is part of the permanent record."""
    path = db.add_entry("PALOMAR-2026-07-29-000002", 1)
    entry = db.read_json(path.relative_to(db.path).as_posix())
    (db.path / entry["verification"]["evidence_path"] / "review.json").unlink()
    errors = validate(db.path)
    assert any("must contain the report, the run provenance, and the review" in e for e in errors)


def test_a_record_citing_another_review_is_rejected(db):
    """A genuine review must not be relabelled onto a different record."""
    path = db.add_entry("PALOMAR-2026-07-29-000002", 1)
    entry = db.read_json(path.relative_to(db.path).as_posix())
    review_path = db.path / entry["verification"]["evidence_path"] / "review.json"
    review = json.loads(review_path.read_text())
    review["decision"] = "reject"
    review_path.write_text(json.dumps(review, indent=2, sort_keys=True) + "\n")
    errors = validate(db.path)
    assert any("review.report.sha256 does not match" in error for error in errors)


def test_unreferenced_verification_evidence_is_rejected(db):
    db.write("evidence/orphan/deadbeef/mechanical-report.json", "{}\n")
    assert any("not referenced by any entry" in error for error in validate(db.path))


def test_dated_identifier_and_acceptance_date_must_agree(db):
    identifier = "PALOMAR-2026-07-29-000002"
    db.add_entry(identifier, 1, accepted_at="2026-07-29")
    assert validate(db.path) == []

    entry = db.entry_data(identifier, 1, accepted_at="2026-07-30")
    db.install_entry(entry)
    errors = validate(db.path)
    assert any("date must match accepted_at" in error for error in errors)


def test_the_acceptance_date_must_be_the_day_version_one_was_registered(db):
    """Two fields that must agree, written in different places, drift.

    `accepted_at` is the result's date: the identifier carries it, browsing
    pages by it, and every later version inherits it. `registered_at` is the
    version's own instant and is what the landing page, the feeds and the
    subject pages order by. They meet at version 1. A record where they have
    come apart is well formed, satisfies its schema and passes the check that
    the identifier matches `accepted_at`; what it produces is a result browsed
    under one day and ordered under another, each correct on its own terms.
    """
    identifier = "PALOMAR-2026-07-29-000002"
    db.add_entry(identifier, 1, registered_at="2026-07-29T11:00:00Z")
    assert validate(db.path) == []

    # A later version keeps the result's date and brings its own instant, which
    # is the whole point of the field being per version.
    db.add_entry(identifier, 2, registered_at="2027-04-01T11:00:00Z")
    assert validate(db.path) == []

    db.install_entry(db.entry_data(identifier, 1, registered_at="2026-07-30T11:00:00Z"))
    errors = validate(db.path)
    assert any(
        f"accepted_at: 2026-07-29 is not the day {identifier}-v1 was registered "
        "(2026-07-30T11:00:00Z)" in error
        for error in errors
    )


def test_a_version_registered_before_its_result_existed_is_refused(db):
    """A later version brings its own instant and inherits the result's date,
    so those two only have to be in order. One that is not sorts behind the
    versions it supersedes, on every surface that carries it."""
    identifier = "PALOMAR-2026-07-29-000002"
    db.add_entry(identifier, 1)
    db.add_entry(identifier, 2, registered_at="2026-07-28T11:00:00Z")

    errors = validate(db.path)
    assert any(
        "registered_at: 2026-07-28T11:00:00Z is before 2026-07-29, "
        "the day the result entered the registry" in error
        for error in errors
    )


def test_a_record_with_no_registration_instant_is_refused(db):
    """Every ordering surface reads it, so a record without one is a record
    they cannot place. The schema is what says so, for every version of it."""
    data = db.entry_data("PALOMAR-2026-07-29-000002", 1)
    del data["registered_at"]
    db.install_entry(data)

    assert any(
        "'registered_at' is a required property" in error for error in validate(db.path)
    )


def test_schema_violations_are_reported(db):
    data = db.entry_data("PALOMAR-2026-07-29-000002", 1)
    del data["trust"]
    db.install_entry(data)
    errors = validate(db.path)
    assert any("'trust' is a required property" in error for error in errors)


@pytest.mark.parametrize(
    ("section", "field"),
    [("verification", "verified_at"), ("review", "reviewed_at")],
)
def test_invalid_rfc3339_timestamp_is_rejected(db, section, field):
    data = db.entry_data("PALOMAR-2026-07-29-000002", 1)
    data[section][field] = "2026-99-99 at noon"
    db.install_entry(data)
    errors = validate(db.path)
    # Canonical UTC to the second, so archived bytes and displayed values agree.
    assert any(f"{section}.{field}" in error and "does not match" in error for error in errors)


def test_malformed_uri_is_rejected_by_format_checker(db):
    data = db.entry_data("PALOMAR-2026-07-29-000002", 1)
    # Passes the https://github.com/ pattern, so only the format checker can
    # catch it: a space is not permitted in a URI.
    data["source"]["repository_url"] = "https://github.com/two words"
    db.install_entry(data)
    errors = validate(db.path)
    assert any("source.repository_url" in error and "is not a 'uri'" in error for error in errors)


def test_source_contact_state_is_not_duplicated_outside_endorsement(db):
    data = db.entry_data("PALOMAR-2026-07-29-000002", 1)
    data["provenance"]["mathematical_sources"][0]["author_contacted"] = "yes"
    db.install_entry(data)
    errors = validate(db.path)
    assert any("author_contacted" in error and "not allowed" in error for error in errors)


def test_descriptive_provenance_fields_accept_bounded_free_text(db):
    data = db.entry_data("PALOMAR-2026-07-29-000002", 1)
    data["provenance"]["mathematical_sources"][0]["author_endorsement"] = (
        "reviewed an early draft"
    )
    data["provenance"]["related_formalizations"] = [{
        "identifier": "https://example.com/formalization",
        "relationship": "shares its computational infrastructure",
    }]
    db.install_entry(data)
    assert validate(db.path) == []


def test_schema_v5_requires_consistent_repository_license_evidence(db):
    data = db.entry_data(
        "PALOMAR-2026-07-29-000002",
        1,
        classification={"arxiv": ["math.CO"], "msc2020": ["05C10"]},
        provenance={
            "result_origin": "original",
            "repository_role": "substantive-development",
            "responsible_maintainers": [{"name": "Ada Lovelace"}],
            "mathematical_sources": [],
            "related_formalizations": [],
        },
    )
    data["submission"]["authorization"] = {"relationship": "maintainer"}
    data["source"]["license"]["detected_identifier"] = "MIT"
    db.install_entry(data)
    errors = validate(db.path)
    assert any("declared_identifier must equal detected_identifier" in error for error in errors)

    data["source"]["license"]["detected_identifier"] = data["source"]["license"][
        "declared_identifier"
    ]
    data["source"]["license"]["path"] = "licenses/LICENSE"
    db.install_entry(data)
    errors = validate(db.path)
    assert any("conventional root licence filename" in error for error in errors)


@pytest.mark.parametrize(
    ("section", "field", "value", "expected"),
    [
        ("verification", "workflow_url", "data:text/html,hostile", "workflow_url"),
        ("verification", "workflow_url", "https://example.com/actions/runs/1", "verification.workflow_url: must be"),
    ],
)
def test_evidence_urls_require_canonical_https_destinations(
    db, section, field, value, expected
):
    data = db.entry_data("PALOMAR-2026-07-29-000002", 1)
    data[section][field] = value
    db.install_entry(data)
    assert any(expected in error for error in validate(db.path))


def test_source_repository_url_is_canonical(db):
    data = db.entry_data("PALOMAR-2026-07-29-000002", 1)
    data["source"]["repository_url"] = "https://github.com/another/repository"
    db.install_entry(data)
    assert any("source.repository_url: must be" in error for error in validate(db.path))


@pytest.mark.parametrize("commit", ["A" * 40, "a" * 39, "main"])
def test_source_commit_is_full_lowercase_hex(db, commit):
    data = db.entry_data("PALOMAR-2026-07-29-000002", 1)
    data["source"]["commit"] = commit
    data["source"]["tree_url"] = data["source"]["repository_url"] + "/tree/" + commit
    db.install_entry(data)
    assert any("source.commit: must be a full lowercase" in error for error in validate(db.path))


def test_source_tree_url_is_derived_from_repository_and_commit(db):
    data = db.entry_data("PALOMAR-2026-07-29-000002", 1)
    data["source"]["tree_url"] = "https://github.com/attacker/wrong/tree/" + "a" * 40
    db.install_entry(data)
    assert any("source.tree_url: must be" in error for error in validate(db.path))


@pytest.mark.parametrize(
    "path",
    [
        ".",
        "../Challenge.lean",
        "/Challenge.lean",
        "https:Challenge.lean",
        "dir\\Challenge.lean",
        "Challenge.lean?raw=1",
        "Challenge.lean#fragment",
    ],
)
def test_challenge_paths_cannot_retarget_outside_source_tree(db, path):
    data = db.entry_data("PALOMAR-2026-07-29-000002", 1)
    data["formalization"]["challenge_path"] = path
    db.install_entry(data)
    assert any("safe repository-relative path" in error for error in validate(db.path))


def test_palomar_indexed_provenance_is_rejected_clearly(db):
    """The sole entry contract rejects the withdrawn trust provenance."""
    data = db.entry_data("PALOMAR-2026-07-29-000002", 1)
    data["formalization"]["project_dependencies"].append(
        {"name": "other", "repository": "someone/other", "revision": "b" * 40}
    )
    data["trust"].update(
        {
            "level": "qualified",
            "challenge_dependencies": [
                {
                    "repository": "someone/other",
                    "provenance": "palomar-indexed",
                    "palomar_id": "PALOMAR-2026-07-29-000001",
                }
            ],
        }
    )
    db.install_entry(data)
    errors = validate(db.path)
    assert any("palomar-indexed is no longer supported" in error for error in errors)



def test_allowlisted_dependency_forbids_palomar_id(db):
    data = db.entry_data("PALOMAR-2026-07-29-000002", 1)
    data["trust"]["challenge_dependencies"][0]["palomar_id"] = "PALOMAR-2026-07-29-000001"
    db.install_entry(data)
    assert any("forbidden for allowlisted" in error for error in validate(db.path))


def test_high_trust_forbids_qualified_allowlisted_dependency(db):
    data = db.entry_data("PALOMAR-2026-07-29-000002", 1)
    data["formalization"]["project_dependencies"].append(
        {"name": "TauCeti", "repository": "FormalFrontier/TauCeti", "revision": "b" * 40}
    )
    data["trust"]["challenge_dependencies"] = [
        {"repository": "TauCetiProject/TauCeti", "provenance": "allowlisted"}
    ]
    db.install_entry(data)
    assert any("high trust permits only" in error for error in validate(db.path))


def test_challenge_dependency_must_be_a_project_dependency(db):
    data = db.entry_data("PALOMAR-2026-07-29-000002", 1)
    data["trust"]["challenge_dependencies"] = [
        {"repository": "someone/not-materialized", "provenance": "allowlisted"}
    ]
    data["trust"]["level"] = "qualified"
    db.install_entry(data)
    assert any("must occur in formalization.project_dependencies" in error for error in validate(db.path))


def test_challenge_dependency_repositories_are_unique(db):
    data = db.entry_data("PALOMAR-2026-07-29-000002", 1)
    data["trust"]["challenge_dependencies"] = [
        {"repository": "leanprover-community/mathlib4", "provenance": "allowlisted"},
        {"repository": "leanprover-community/mathlib4", "provenance": "allowlisted"},
    ]
    data["trust"]["level"] = "qualified"
    db.install_entry(data)
    assert any("challenge repository is duplicated" in error for error in validate(db.path))


def test_classification_is_validated_when_present(db):
    db.add_entry("PALOMAR-2026-07-29-000002", 1)
    data = db.entry_data("PALOMAR-2026-07-29-000002", 1)
    data["classification"] = {
        "arxiv": ["math.CO", "math.NT"],
        "msc2020": ["05C10"],
    }
    db.install_entry(data)
    assert validate(db.path) == []

    data["classification"]["arxiv"].append("cs.LO")
    db.install_entry(data)
    errors = validate(db.path)
    assert any("classification.arxiv" in error and "too long" in error for error in errors)


def test_retired_root_index_is_rejected(db):
    db.write_json("index.json", {"schema_version": 2, "entries": []})
    assert any("retired whole-registry authority" in error for error in validate(db.path))


def test_symlinked_entry_is_rejected(db):
    db.write_json("payload.json", db.entry_data("PALOMAR-2026-07-29-000002", 1))
    (db.path / "entries" / "PALOMAR-2026-07-29-000002-v1.json").symlink_to("../payload.json")
    errors = validate(db.path)
    assert any("not symbolic links" in error for error in errors)


def test_stray_file_in_entries_is_rejected(db):
    db.write("entries/notes.txt", "hello\n")
    errors = validate(db.path)
    assert any("entries/notes.txt" in error for error in errors)


def test_malformed_entry_is_reported(db):
    db.write("entries/PALOMAR-2026-07-29-000002-v1.json", "{ not json\n")
    errors = validate(db.path)
    assert any("invalid JSON" in error for error in errors)


def test_render_artifact_content_address_is_verified(db):
    entry = db.entry_data("PALOMAR-2026-07-29-000001", 1)
    page = db.path / entry["challenge_render"]["artifact_path"] / "Challenge" / "index.html"
    page.write_text("tampered", encoding="utf-8")
    errors = validate(db.path)
    assert any("content hashes to" in error for error in errors)
    assert any("artifact manifest does not match" in error for error in errors)


def test_render_policy_rejects_extra_javascript_even_when_content_addressed(db):
    data = db.entry_data("PALOMAR-2026-07-29-000001", 1)
    bundle = db.path / data["challenge_render"]["artifact_path"]
    (bundle / "hostile.js").write_text("alert(document.cookie)\n", encoding="utf-8")
    readdress_render(db, data)
    assert any("untrusted JavaScript file: hostile.js" in error for error in validate(db.path))


def test_render_policy_rejects_modified_trusted_runtime_even_when_content_addressed(db):
    data = db.entry_data("PALOMAR-2026-07-29-000001", 1)
    bundle = db.path / data["challenge_render"]["artifact_path"]
    with (bundle / "palomar-sanitize.js").open("a", encoding="utf-8") as handle:
        handle.write("// modified\n")
    readdress_render(db, data)
    assert any(
        "trusted runtime bytes do not match: palomar-sanitize.js" in error
        for error in validate(db.path)
    )


def test_render_policy_rejects_missing_csp_even_when_content_addressed(db):
    data = db.entry_data("PALOMAR-2026-07-29-000001", 1)
    bundle = db.path / data["challenge_render"]["artifact_path"]
    page = bundle / "Challenge" / "index.html"
    page.write_text(
        page.read_text(encoding="utf-8").replace(
            '<meta http-equiv="Content-Security-Policy"',
            '<meta http-equiv="X-Removed-Content-Security-Policy"',
        ),
        encoding="utf-8",
    )
    readdress_render(db, data)
    assert any("exactly one active trusted CSP" in error for error in validate(db.path))


def test_render_policy_does_not_accept_csp_text_inside_a_comment(db):
    data = db.entry_data("PALOMAR-2026-07-29-000001", 1)
    bundle = db.path / data["challenge_render"]["artifact_path"]
    page = bundle / "Challenge" / "index.html"
    html = page.read_text(encoding="utf-8")
    page.write_text(
        html.replace(
            RENDER_CSP_META,
            f"<!-- {RENDER_CSP_META} -->"
            '<meta http-equiv="Content-Security-Policy" content="default-src *">',
        ),
        encoding="utf-8",
    )
    readdress_render(db, data)
    assert any("exactly one active trusted CSP" in error for error in validate(db.path))


def test_render_policy_requires_csp_in_the_active_head(db):
    data = db.entry_data("PALOMAR-2026-07-29-000001", 1)
    bundle = db.path / data["challenge_render"]["artifact_path"]
    page = bundle / "Challenge" / "index.html"
    html = page.read_text(encoding="utf-8").replace(RENDER_CSP_META, "")
    page.write_text(html.replace("</body>", f"{RENDER_CSP_META}</body>"), encoding="utf-8")
    readdress_render(db, data)
    assert any("exactly one active trusted CSP" in error for error in validate(db.path))


@pytest.mark.parametrize(
    "payload",
    [
        "<script>alert(document.domain)</script>",
        '<img src="#" onerror="alert(document.domain)">',
        '<iframe srcdoc="active"></iframe>',
        '<script defer src="../xref.json"></script>',
    ],
)
def test_render_policy_rejects_active_html_even_when_content_addressed(db, payload):
    data = db.entry_data("PALOMAR-2026-07-29-000001", 1)
    bundle = db.path / data["challenge_render"]["artifact_path"]
    page = bundle / "Challenge" / "index.html"
    page.write_text(
        page.read_text(encoding="utf-8").replace("</body>", f"{payload}</body>"),
        encoding="utf-8",
    )
    readdress_render(db, data)
    assert any(
        marker in error
        for error in validate(db.path)
        for marker in ("inline script", "active HTML", "untrusted script source")
    )


@pytest.mark.parametrize("wrapper", [("<title>", "</title>"), ("<svg>", "</svg>")])
def test_render_policy_rejects_browser_parser_divergence_before_csp(db, wrapper):
    data = db.entry_data("PALOMAR-2026-07-29-000001", 1)
    bundle = db.path / data["challenge_render"]["artifact_path"]
    page = bundle / "Challenge" / "index.html"
    before, after = wrapper
    page.write_text(
        page.read_text(encoding="utf-8").replace(
            RENDER_CSP_META,
            f"{before}{RENDER_CSP_META}{after}",
        ),
        encoding="utf-8",
    )
    readdress_render(db, data)
    assert any("before the trusted CSP" in error for error in validate(db.path))


def test_render_policy_requires_both_trusted_runtimes_in_order(db):
    data = db.entry_data("PALOMAR-2026-07-29-000001", 1)
    bundle = db.path / data["challenge_render"]["artifact_path"]
    page = bundle / "Challenge" / "index.html"
    html = page.read_text(encoding="utf-8")
    first = '<script defer src="../palomar-sanitize.js"></script>'
    second = '<script defer src="../palomar-verso.js"></script>'
    page.write_text(html.replace(first, "").replace(second, first), encoding="utf-8")
    readdress_render(db, data)
    assert any("each trusted runtime exactly once and in order" in error for error in validate(db.path))


def test_render_policy_rejects_disallowed_extension_even_when_content_addressed(db):
    data = db.entry_data("PALOMAR-2026-07-29-000001", 1)
    bundle = db.path / data["challenge_render"]["artifact_path"]
    (bundle / "payload.wasm").write_bytes(b"not really wasm")
    readdress_render(db, data)
    assert any("disallowed artifact extension: payload.wasm" in error for error in validate(db.path))


def test_missing_render_artifact_is_rejected(db):
    entry = db.entry_data("PALOMAR-2026-07-29-000001", 1)
    artifact = db.path / entry["challenge_render"]["artifact_path"]
    shutil.rmtree(artifact)
    errors = validate(db.path)
    assert any("artifact directory is missing" in error for error in errors)


def test_render_artifact_size_limit_is_rechecked(db, monkeypatch):
    entry = db.entry_data("PALOMAR-2026-07-29-000001", 1)
    page = db.path / entry["challenge_render"]["artifact_path"] / "Challenge" / "index.html"
    monkeypatch.setattr(
        "render_validation.MAX_RENDER_FILE_BYTES", page.stat().st_size - 1
    )
    errors = validate(db.path)
    assert any("artifact file exceeds the size cap" in error for error in errors)


def test_unreferenced_render_artifact_is_rejected(db):
    db.write("renders/unreferenced/index.html", "not in an entry\n")
    errors = validate(db.path)
    assert any("render artifact is not referenced by any entry" in error for error in errors)


def test_database_wide_render_node_limit_is_rechecked(db, monkeypatch):
    monkeypatch.setattr("render_validation.MAX_RENDER_NODES", 1)
    errors = validate(db.path)
    assert any("database-wide filesystem-node cap" in error for error in errors)


def test_database_wide_evidence_node_limit_comes_from_its_owner(db, monkeypatch):
    monkeypatch.setattr("evidence_validation.MAX_EVIDENCE_NODES", 1)
    errors = validate(db.path)
    assert any(
        error.startswith("evidence/: exceeds the database-wide filesystem-node cap")
        for error in errors
    )


# Legacy evidence: post-hoc capture for entries published before schema v5.


# The captured review comment must evidence the entry, not merely exist.

# Palomar moved to an organisation; both submission repositories are canonical.


def test_validation_says_when_append_only_enforcement_is_off(db, capsys):
    """The other half of the same sentence.

    This is the job a reader opens, and the two green checks are easy to
    confuse: this one says every record is well formed, which is all it has
    ever said. While the marker is absent, nothing says any of them will still
    be there tomorrow.
    """
    from validate import main

    db.remove(".palomar-launched")
    assert main(["--root", str(db.path)]) == 0
    printed = capsys.readouterr().out
    assert "::warning::" in printed
    assert "enforcement is OFF" in printed
    assert "database is valid" in printed


def test_validation_says_nothing_about_it_once_the_marker_is_there(db, capsys):
    from validate import main

    assert main(["--root", str(db.path)]) == 0
    assert "::warning::" not in capsys.readouterr().out
