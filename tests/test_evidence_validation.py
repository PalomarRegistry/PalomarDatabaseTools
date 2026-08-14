"""Direct tests for immutable verification-evidence validation."""

from __future__ import annotations

import copy
import shutil

import evidence_validation


def _canonical_entry(db):
    return db.read_json("entries/PALOMAR-2026-07-29-000001-v1.json")


def _bundle(db, entry):
    return db.path / entry["verification"]["evidence_path"]


def test_the_canonical_bundle_passes_without_mutating_its_entry(db):
    entry = _canonical_entry(db)
    before = copy.deepcopy(entry)

    assert evidence_validation.validate_evidence(db.path, entry, "entry.json") == []
    assert entry == before


def test_evidence_path_is_bound_before_the_bundle_is_read(db):
    entry = _canonical_entry(db)
    tree_hash = entry["verification"]["evidence_tree_sha256"]
    entry["verification"]["evidence_path"] = "evidence/somewhere-else/"

    assert evidence_validation.validate_evidence(db.path, entry, "entry.json") == [
        "entry.json:verification.evidence_path: must be "
        f"evidence/PALOMAR-2026-07-29-000001-v1/{tree_hash}/"
    ]


def test_missing_bundle_fails_at_the_evidence_boundary(db):
    entry = _canonical_entry(db)
    shutil.rmtree(db.path / entry["verification"]["evidence_path"])

    assert evidence_validation.validate_evidence(db.path, entry, "entry.json") == [
        "entry.json:verification evidence directory is missing or symbolic"
    ]


def test_symbolic_path_fails_before_any_evidence_file_is_read(db):
    entry = _canonical_entry(db)
    evidence = db.path / "evidence"
    actual = db.path / "evidence-real"
    evidence.rename(actual)
    evidence.symlink_to(actual, target_is_directory=True)

    assert evidence_validation.validate_evidence(db.path, entry, "entry.json") == [
        "entry.json:verification evidence path contains a symbolic link"
    ]


def test_malformed_evidence_shape_is_left_to_the_entry_schema(db):
    entry = _canonical_entry(db)
    entry["verification"] = []

    assert evidence_validation.validate_evidence(db.path, entry, "entry.json") == []


def test_malformed_evidence_identity_is_left_to_the_entry_schema(db):
    entry = _canonical_entry(db)
    entry["version"] = True

    assert evidence_validation.validate_evidence(db.path, entry, "entry.json") == []


def test_an_unexpected_file_is_rejected_even_when_the_manifest_still_matches(db):
    entry = _canonical_entry(db)
    (_bundle(db, entry) / "extra.json").write_text("{}\n", encoding="utf-8")

    assert evidence_validation.validate_evidence(db.path, entry, "entry.json") == [
        "entry.json:verification evidence contains an unexpected file: extra.json"
    ]


def test_a_directory_inside_the_closed_bundle_is_rejected(db):
    entry = _canonical_entry(db)
    (_bundle(db, entry) / "nested").mkdir()

    assert evidence_validation.validate_evidence(db.path, entry, "entry.json") == [
        "entry.json:verification evidence contains an unexpected directory: nested"
    ]


def test_a_symbolic_file_inside_the_bundle_is_rejected(db):
    entry = _canonical_entry(db)
    bundle = _bundle(db, entry)
    (bundle / "review-copy.json").symlink_to(bundle / "review.json")

    assert evidence_validation.validate_evidence(db.path, entry, "entry.json") == [
        "entry.json:verification evidence contains a symbolic link: review-copy.json"
    ]


def test_database_wide_node_budget_is_derived_from_the_closed_bundle_shape():
    assert evidence_validation.MAX_EVIDENCE_NODES == len(
        evidence_validation.EVIDENCE_FILES
    ) + 2


def test_file_cap_is_enforced_without_validation_orchestration(db, monkeypatch):
    entry = _canonical_entry(db)
    report = (
        db.path
        / entry["verification"]["evidence_path"]
        / "mechanical-report.json"
    )
    monkeypatch.setattr(
        evidence_validation, "MAX_EVIDENCE_FILE_BYTES", report.stat().st_size - 1
    )

    errors = evidence_validation.validate_evidence(db.path, entry, "entry.json")

    assert any("evidence file exceeds the size cap" in error for error in errors)


def test_total_cap_is_enforced_without_validation_orchestration(db, monkeypatch):
    entry = _canonical_entry(db)
    monkeypatch.setattr(evidence_validation, "MAX_EVIDENCE_BYTES", 0)

    errors = evidence_validation.validate_evidence(db.path, entry, "entry.json")

    assert any("evidence exceeds the total-size cap" in error for error in errors)


def test_digest_and_manifest_bindings_are_enforced_at_the_boundary(db):
    entry = _canonical_entry(db)
    report = (
        db.path
        / entry["verification"]["evidence_path"]
        / "mechanical-report.json"
    )
    report.write_text("{}\n", encoding="utf-8")

    errors = evidence_validation.validate_evidence(db.path, entry, "entry.json")

    assert any("evidence_tree_sha256: content hashes to" in error for error in errors)
    assert any("evidence manifest does not match its files" in error for error in errors)
    assert any("mechanical_report_sha256 does not match" in error for error in errors)
