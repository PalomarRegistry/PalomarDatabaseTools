#!/usr/bin/env python3
"""Rewrite review, State, and Database records to the problem-detection vocabulary."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys
from typing import Any


OUTCOMES = {
    "accept": "neutral",
    "revise": "revision_required",
    "reject": "rejected",
}
CHECK_OUTCOMES = {"pass": "neutral", "warn": "warning", "fail": "failure"}
MIGRATION_PATH = pathlib.Path("migrations/review-language-v3.json")
TEXT_REPLACEMENTS = (
    ("The strongest case against acceptance", "The strongest potential problem"),
    ("the strongest case against acceptance", "the strongest potential problem"),
    ("before acceptance", "before registration is permitted"),
    ("the acceptance threshold", "the review threshold"),
    ("acceptance threshold", "review threshold"),
    ("Acceptance is blocked by", "Registration is blocked by"),
    (
        "all completed editorial passes support acceptance",
        "all completed editorial checks identified no blocking problem",
    ),
    (
        "All completed editorial passes support acceptance",
        "All completed editorial checks identified no blocking problem",
    ),
    (
        "all completed review passes met the review threshold",
        "all completed review checks met the review threshold",
    ),
    ("an accepted authorization basis", "a permitted authorization basis"),
    (
        "an accepted, truthful authorization declaration",
        "a permitted, truthful authorization declaration",
    ),
)


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: must contain a JSON object")
    return value


def write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.migration-tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def migrate_text(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(child, str):
                for old, new in TEXT_REPLACEMENTS:
                    child = child.replace(old, new)
                value[key] = child
            else:
                migrate_text(child)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            if isinstance(child, str):
                for old, new in TEXT_REPLACEMENTS:
                    child = child.replace(old, new)
                value[index] = child
            else:
                migrate_text(child)


def migrate_review(review: dict[str, Any]) -> dict[str, Any]:
    migrated = dict(review)
    migrated["schema_version"] = 3
    if "decision" in migrated:
        migrated["outcome"] = OUTCOMES.get(migrated.pop("decision"), migrated.get("outcome"))
    passes = migrated.pop("passes", None)
    if passes is not None:
        migrated["checks"] = passes
    checks = migrated.get("checks", [])
    if isinstance(checks, list):
        for item in checks:
            if not isinstance(item, dict):
                continue
            if "verdict" in item:
                item["outcome"] = CHECK_OUTCOMES.get(
                    item.pop("verdict"), item.get("outcome")
                )
    migrate_text(migrated)
    return migrated


def evidence_files(bundle: pathlib.Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(bundle.iterdir()):
        if path.name == "evidence-manifest.json":
            continue
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"{path}: evidence bundles must contain ordinary files")
        data = path.read_bytes()
        rows.append(
            {"path": path.name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        )
    return rows


def migrate_database(root: pathlib.Path) -> list[dict[str, str]]:
    sys.path.insert(0, str(root / "tools"))
    import registration_projection  # type: ignore

    changes: list[dict[str, str]] = []
    entries: list[tuple[str, dict[str, Any]]] = []
    for entry_path in sorted((root / "entries").glob("*.json")):
        relative = entry_path.relative_to(root).as_posix()
        old_entry_sha = file_digest(entry_path)
        entry = read_json(entry_path)
        verification = entry["verification"]
        old_tree = verification["evidence_tree_sha256"]
        old_bundle = root / verification["evidence_path"]
        old_review_sha = file_digest(old_bundle / "review.json")

        review = migrate_review(read_json(old_bundle / "review.json"))
        write_json(old_bundle / "review.json", review)
        new_review_sha = file_digest(old_bundle / "review.json")
        files = evidence_files(old_bundle)
        encoded = json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
        new_tree = hashlib.sha256(encoded).hexdigest()
        manifest = {
            "schema_version": 1,
            "evidence_tree_sha256": new_tree,
            "files": files,
        }
        write_json(old_bundle / "evidence-manifest.json", manifest)
        new_bundle = old_bundle.parent / new_tree
        if old_bundle != new_bundle:
            if new_bundle.exists():
                raise ValueError(f"{new_bundle}: migration target already exists")
            old_bundle.rename(new_bundle)

        entry["schema_version"] = 3
        if "accepted_at" in entry:
            entry["first_registered_on"] = entry.pop("accepted_at")
        entry["status"] = "registered"
        recorded_review = entry["review"]
        if "verdict" in recorded_review:
            recorded_review["outcome"] = OUTCOMES.get(
                recorded_review.pop("verdict"), recorded_review.get("outcome")
            )
        migrate_text(recorded_review)
        recorded_review["report"]["sha256"] = new_review_sha
        verification["evidence_tree_sha256"] = new_tree
        verification["evidence_path"] = (
            f"evidence/{entry['id']}-v{entry['version']}/{new_tree}/"
        )
        write_json(entry_path, entry)
        new_entry_sha = file_digest(entry_path)
        entries.append((relative, entry))
        if (old_entry_sha, old_review_sha, old_tree) != (
            new_entry_sha,
            new_review_sha,
            new_tree,
        ):
            changes.append(
                {
                    "entry": relative,
                    "old_entry_sha256": old_entry_sha,
                    "new_entry_sha256": new_entry_sha,
                    "old_review_sha256": old_review_sha,
                    "new_review_sha256": new_review_sha,
                    "old_evidence_tree_sha256": old_tree,
                    "new_evidence_tree_sha256": new_tree,
                }
            )

    projections = registration_projection.documents_for_entries(entries, enforce_contract=True)
    for relative, document in projections.items():
        write_json(root / relative, document)
    return changes


def replace_digest_bindings(value: Any, old: str, new: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.endswith("review_sha256") and child == old:
                value[key] = new
            else:
                replace_digest_bindings(child, old, new)
    elif isinstance(value, list):
        for child in value:
            replace_digest_bindings(child, old, new)


def migrate_state(root: pathlib.Path) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    for review_path in sorted((root / "submissions").glob("*/review.json")):
        submission_id = review_path.parent.name
        review = read_json(review_path)
        old_digest = canonical_digest(review)
        migrated = migrate_review(review)
        new_digest = canonical_digest(migrated)
        write_json(review_path, migrated)
        state_path = review_path.with_name("state.json")
        state = read_json(state_path)
        replace_digest_bindings(state, old_digest, new_digest)
        state["review_sha256"] = new_digest
        state["review_schema_version"] = 3
        attempt = state.get("registration_attempt")
        if isinstance(attempt, dict):
            attempt["schema_version"] = 2
            if "accepted_at" in attempt:
                attempt["first_registered_on"] = attempt.pop("accepted_at")
        write_json(state_path, state)
        if old_digest != new_digest:
            changes.append(
                {
                    "submission_id": submission_id,
                    "old_review_sha256": old_digest,
                    "new_review_sha256": new_digest,
                }
            )
    return changes


def record_manifest(root: pathlib.Path, kind: str, rows: list[dict[str, str]]) -> None:
    path = root / MIGRATION_PATH
    if path.is_file():
        manifest = read_json(path)
        existing_rows = manifest.get(kind, [])
        if not isinstance(existing_rows, list):
            raise ValueError(f"{path}: {kind} must be a list")
        identity = "entry" if kind == "database_changes" else "submission_id"
        combined = {row[identity]: dict(row) for row in existing_rows}
        for row in rows:
            current = combined.get(row[identity])
            if current is None:
                combined[row[identity]] = dict(row)
                continue
            for key, value in row.items():
                if key.startswith("new_"):
                    current[key] = value
        rows = [combined[key] for key in sorted(combined)]
    write_json(path, {"schema_version": 1, "migration": "review-language-v3", kind: rows})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=pathlib.Path)
    parser.add_argument("--state", type=pathlib.Path)
    args = parser.parse_args()
    if (args.database is None) == (args.state is None):
        parser.error("pass exactly one of --database or --state")
    if args.database is not None:
        root = args.database.resolve()
        record_manifest(root, "database_changes", migrate_database(root))
    else:
        root = args.state.resolve()
        record_manifest(root, "state_changes", migrate_state(root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
