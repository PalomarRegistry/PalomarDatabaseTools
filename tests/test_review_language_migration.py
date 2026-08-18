"""The one-time review-language rewrite remains auditable and repeatable."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate_review_language.py"
SPEC = importlib.util.spec_from_file_location("migrate_review_language", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


def test_review_rewrite_changes_structure_and_human_language() -> None:
    review = {
        "schema_version": 2,
        "decision": "accept",
        "summary": "All completed editorial passes support acceptance.",
        "passes": [
            {
                "verdict": "pass",
                "findings": [
                    {"message": "The strongest case against acceptance is narrow scope."}
                ],
            }
        ],
    }

    rewritten = migration.migrate_review(review)

    assert rewritten["schema_version"] == 3
    assert rewritten["outcome"] == "neutral"
    assert "decision" not in rewritten and "passes" not in rewritten
    assert rewritten["checks"][0]["outcome"] == "neutral"
    assert "verdict" not in rewritten["checks"][0]
    assert rewritten["summary"] == (
        "All completed editorial checks identified no blocking problem."
    )
    assert rewritten["checks"][0]["findings"][0]["message"] == (
        "The strongest potential problem is narrow scope."
    )


def test_manifest_keeps_original_hashes_and_advances_final_hashes(tmp_path: Path) -> None:
    original = {
        "entry": "entries/PALOMAR-2026-08-18-000001-v1.json",
        "old_entry_sha256": "a" * 64,
        "new_entry_sha256": "b" * 64,
        "old_review_sha256": "c" * 64,
        "new_review_sha256": "d" * 64,
        "old_evidence_tree_sha256": "e" * 64,
        "new_evidence_tree_sha256": "f" * 64,
    }
    final = {
        **original,
        "old_entry_sha256": "b" * 64,
        "new_entry_sha256": "1" * 64,
        "old_review_sha256": "d" * 64,
        "new_review_sha256": "2" * 64,
        "old_evidence_tree_sha256": "f" * 64,
        "new_evidence_tree_sha256": "3" * 64,
    }

    migration.record_manifest(tmp_path, "database_changes", [original])
    migration.record_manifest(tmp_path, "database_changes", [final])
    migration.record_manifest(tmp_path, "database_changes", [])

    manifest = json.loads((tmp_path / migration.MIGRATION_PATH).read_text())
    [row] = manifest["database_changes"]
    assert row["old_entry_sha256"] == "a" * 64
    assert row["new_entry_sha256"] == "1" * 64
    assert row["old_review_sha256"] == "c" * 64
    assert row["new_review_sha256"] == "2" * 64
    assert row["old_evidence_tree_sha256"] == "e" * 64
    assert row["new_evidence_tree_sha256"] == "3" * 64
