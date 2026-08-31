from __future__ import annotations

import hashlib
import json

from correction_validation import _active_baseline_version, correction_errors


ID = "PALOMAR-2026-08-31-000001"


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _fixture(root):
    _write_json(root / "takedowns.json", {"schema_version": 1, "takedowns": []})
    baseline = {
        "schema_version": 3,
        "id": ID,
        "version": 1,
        "status": "registered",
        "registered_at": "2026-08-31T00:00:00Z",
        "title": "Transcripton",
        "abstract": "A result.",
        "authors": [{"name": "Ada"}],
        "classification": {"arxiv": ["math.LO"], "msc2020": ["03B35"]},
        "source": {"repository": "owner/repo", "commit": "1" * 40},
        "formalization": {"comparator_config_path": "Comparator/comparator.json"},
        "verification": {"evidence_path": "evidence/original/"},
        "challenge_render": {"artifact_path": "renders/original/"},
        "preservation": {"repositories": []},
        "trust": {"level": "qualified"},
        "provenance": {
            "result_origin": "original",
            "responsible_maintainers": [{"name": "Ada"}],
            "mathematical_sources": [],
            "related_formalizations": [],
        },
        "review": {"outcome": "neutral"},
        "submission": {"authorization": {"relationship": "repository-maintainer"}},
    }
    baseline_path = root / f"entries/{ID}-v1.json"
    _write_json(baseline_path, baseline)
    baseline_sha = hashlib.sha256(baseline_path.read_bytes()).hexdigest()
    based_on = {
        "version": 1,
        "path": f"entries/{ID}-v1.json",
        "sha256": baseline_sha,
    }
    entry = json.loads(json.dumps(baseline))
    entry.update({
        "schema_version": 4,
        "version": 2,
        "registered_at": "2026-08-31T01:00:00Z",
        "title": "Transcription",
        "review": {"outcome": "neutral"},
        "submission": {"authorization": {"relationship": "palomar-maintainer"}},
    })
    correction = {
        "kind": "registry-metadata-correction",
        "generated_by": "Palomar / Registry correction",
        "based_on": based_on,
        "explanation": "Correct a transcription error in the title.",
        "changed_fields": ["title"],
    }
    files = {
        "baseline-reference.json": {
            "schema_version": 1,
            "id": ID,
            **based_on,
            "inherited": [
                "source", "formalization", "verification", "challenge_render",
                "preservation", "trust",
            ],
        },
        "correction-report.json": {
            "schema_version": 2,
            "status": "pass",
            "stage": "correction-validation",
            "submission": {"registry_correction": {
                "explanation": correction["explanation"],
                "changed_fields": correction["changed_fields"],
                "baseline": {"id": ID, **based_on},
            }},
        },
        "workflow-run.json": {"schema_version": 1},
        "review.json": {"schema_version": 3},
    }
    encoded = {
        name: (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
        for name, value in files.items()
    }
    manifest_files = [
        {"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        for name, data in sorted(encoded.items())
    ]
    tree_hash = hashlib.sha256(
        json.dumps(manifest_files, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    bundle = root / f"evidence/{ID}-v2/{tree_hash}"
    for name, data in encoded.items():
        path = bundle / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    _write_json(bundle / "evidence-manifest.json", {
        "schema_version": 2,
        "evidence_tree_sha256": tree_hash,
        "files": manifest_files,
    })
    correction.update({
        "evidence_path": f"evidence/{ID}-v2/{tree_hash}/",
        "evidence_tree_sha256": tree_hash,
    })
    entry["registry_correction"] = correction
    return entry


def test_a_correction_binds_the_immediate_baseline_and_exact_public_delta(tmp_path):
    entry = _fixture(tmp_path)
    assert correction_errors(tmp_path, f"entries/{ID}-v2.json", entry) == []


def test_a_correction_cannot_change_repository_or_commit(tmp_path):
    entry = _fixture(tmp_path)
    entry["source"]["commit"] = "2" * 40
    errors = correction_errors(tmp_path, f"entries/{ID}-v2.json", entry)
    assert any(":source: registry corrections must inherit" in error for error in errors)


def test_changed_fields_is_derived_instead_of_trusted(tmp_path):
    entry = _fixture(tmp_path)
    entry["registry_correction"]["changed_fields"] = ["abstract"]
    errors = correction_errors(tmp_path, f"entries/{ID}-v2.json", entry)
    assert any("changed_fields: must exactly describe" in error for error in errors)


def test_the_active_baseline_may_precede_a_taken_down_latest_version(tmp_path):
    for version in (1, 2):
        _write_json(tmp_path / f"entries/{ID}-v{version}.json", {"version": version})
    _write_json(tmp_path / "takedowns.json", {
        "schema_version": 1,
        "takedowns": [{
            "id": ID,
            "version": 2,
            "taken_down_at": "2026-08-31T00:00:00Z",
            "authorized_by_login": "avigad",
            "authorization_issue": 1,
            "reason": "Temporarily removed.",
        }],
    })
    assert _active_baseline_version(tmp_path, ID, 3) == 1
