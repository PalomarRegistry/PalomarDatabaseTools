"""Cross-version invariants for exceptional registry metadata corrections."""

from __future__ import annotations

import hashlib
import json
import pathlib
import stat
from collections.abc import Mapping
from typing import Any


CORRECTABLE_FIELDS = (
    "title",
    "abstract",
    "authors",
    "classification.arxiv",
    "classification.msc2020",
    "provenance.responsible_maintainers",
    "provenance.mathematical_sources",
    "provenance.related_formalizations",
)
MUTABLE_TOP_LEVEL = {
    "schema_version",
    "registered_at",
    "version",
    "status",
    "title",
    "abstract",
    "authors",
    "classification",
    "provenance",
    "review",
    "submission",
    "registry_correction",
}


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _field(value: Mapping[str, Any], path: str) -> object:
    current: object = value
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _active_baseline_version(root: pathlib.Path, identifier: object, before: int) -> int | None:
    try:
        manifest = json.loads((root / "takedowns.json").read_bytes())
    except (OSError, json.JSONDecodeError):
        return None
    rows = manifest.get("takedowns") if isinstance(manifest, dict) else None
    if not isinstance(rows, list):
        return None
    inactive = {
        row.get("version")
        for row in rows
        if isinstance(row, dict) and row.get("id") == identifier
        and type(row.get("version")) is int
    }
    candidates = []
    for path in (root / "entries").glob(f"{identifier}-v*.json"):
        match = path.name.removeprefix(f"{identifier}-v").removesuffix(".json")
        if match.isdigit() and 1 <= int(match) < before and int(match) not in inactive:
            candidates.append(int(match))
    return max(candidates) if candidates else None


def correction_errors(root: pathlib.Path, name: str, entry: Mapping[str, Any]) -> list[str]:
    """Require an exact active baseline and prohibit every non-metadata change."""
    correction = _mapping(entry.get("registry_correction"))
    if not correction:
        return []
    errors: list[str] = []
    based_on = _mapping(correction.get("based_on"))
    identifier = entry.get("id")
    version = entry.get("version")
    expected_version = (
        _active_baseline_version(root, identifier, version)
        if isinstance(version, int) and not isinstance(version, bool)
        else None
    )
    expected_path = f"entries/{identifier}-v{expected_version}.json"
    if based_on.get("version") != expected_version or based_on.get("path") != expected_path:
        errors.append(
            f"{name}:registry_correction.based_on: must name the exact active baseline version"
        )
        return errors
    baseline_path = root / expected_path
    try:
        baseline_bytes = baseline_path.read_bytes()
        baseline = json.loads(baseline_bytes)
    except (OSError, json.JSONDecodeError):
        errors.append(f"{name}:registry_correction.based_on: baseline entry is unavailable")
        return errors
    if hashlib.sha256(baseline_bytes).hexdigest() != based_on.get("sha256"):
        errors.append(f"{name}:registry_correction.based_on.sha256: does not bind baseline bytes")
    if baseline.get("id") != identifier or baseline.get("version") != expected_version:
        errors.append(f"{name}:registry_correction.based_on: baseline identity disagrees")
    if baseline.get("status") != "registered" or entry.get("status") != "registered":
        errors.append(f"{name}: registry corrections require registered canonical entries")

    for key in sorted(set(entry) | set(baseline)):
        if key not in MUTABLE_TOP_LEVEL and entry.get(key) != baseline.get(key):
            errors.append(f"{name}:{key}: registry corrections must inherit this field exactly")

    baseline_provenance = _mapping(baseline.get("provenance"))
    provenance = _mapping(entry.get("provenance"))
    for key in sorted(set(baseline_provenance) | set(provenance)):
        if key not in {
            "responsible_maintainers", "mathematical_sources", "related_formalizations"
        } and provenance.get(key) != baseline_provenance.get(key):
            errors.append(
                f"{name}:provenance.{key}: registry corrections must inherit this field exactly"
            )

    changed = [
        field for field in CORRECTABLE_FIELDS
        if _field(entry, field) != _field(baseline, field)
    ]
    if correction.get("changed_fields") != changed:
        errors.append(
            f"{name}:registry_correction.changed_fields: must exactly describe effective changes"
        )
    if entry.get("submission", {}).get("authorization", {}).get("relationship") != "palomar-maintainer":
        errors.append(f"{name}:submission.authorization.relationship: must be palomar-maintainer")
    errors.extend(_evidence_errors(root, name, entry, correction))
    return errors


def _evidence_errors(
    root: pathlib.Path,
    name: str,
    entry: Mapping[str, Any],
    correction: Mapping[str, Any],
) -> list[str]:
    errors: list[str] = []
    identifier, version = entry.get("id"), entry.get("version")
    tree_hash = correction.get("evidence_tree_sha256")
    expected_path = f"evidence/{identifier}-v{version}/{tree_hash}/"
    if correction.get("evidence_path") != expected_path:
        return [f"{name}:registry_correction.evidence_path: must be {expected_path}"]
    bundle = root / expected_path
    if bundle.is_symlink() or not bundle.is_dir():
        return [f"{name}:registry correction evidence directory is missing or symbolic"]
    expected_names = {
        "baseline-reference.json", "correction-report.json", "workflow-run.json",
        "review.json", "evidence-manifest.json",
    }
    paths = list(bundle.iterdir())
    if any(path.is_symlink() or not stat.S_ISREG(path.stat().st_mode) for path in paths):
        errors.append(f"{name}:registry correction evidence must contain ordinary files only")
        return errors
    if {path.name for path in paths} != expected_names:
        errors.append(f"{name}:registry correction evidence has an unsupported file set")
        return errors
    files = [
        {"path": path.name, "bytes": path.stat().st_size,
         "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in sorted(paths)
        if path.name != "evidence-manifest.json"
    ]
    calculated = hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    try:
        manifest = json.loads((bundle / "evidence-manifest.json").read_bytes())
        baseline_reference = json.loads((bundle / "baseline-reference.json").read_bytes())
        report = json.loads((bundle / "correction-report.json").read_bytes())
    except (OSError, json.JSONDecodeError):
        return [f"{name}:registry correction evidence contains invalid JSON"]
    if calculated != tree_hash or manifest != {
        "schema_version": 2, "evidence_tree_sha256": calculated, "files": files
    }:
        errors.append(f"{name}:registry correction evidence manifest or tree hash disagrees")
    based_on = correction.get("based_on")
    if not isinstance(based_on, dict) or baseline_reference != {
        "schema_version": 1,
        "id": identifier,
        "version": based_on.get("version") if isinstance(based_on, dict) else None,
        "path": based_on.get("path") if isinstance(based_on, dict) else None,
        "sha256": based_on.get("sha256") if isinstance(based_on, dict) else None,
        "inherited": [
            "source", "formalization", "verification", "challenge_render",
            "preservation", "trust",
        ],
    }:
        errors.append(f"{name}:baseline-reference.json disagrees with the correction")
    reported = _mapping(_mapping(report.get("submission")).get("registry_correction"))
    if (
        report.get("schema_version") != 2
        or report.get("status") != "pass"
        or report.get("stage") != "correction-validation"
        or reported.get("explanation") != correction.get("explanation")
        or reported.get("changed_fields") != correction.get("changed_fields")
        or reported.get("baseline") != {
            "id": identifier,
            "version": based_on.get("version") if isinstance(based_on, dict) else None,
            "path": based_on.get("path") if isinstance(based_on, dict) else None,
            "sha256": based_on.get("sha256") if isinstance(based_on, dict) else None,
        }
    ):
        errors.append(f"{name}:correction-report.json disagrees with the registered correction")
    return errors
