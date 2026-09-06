"""Cross-version invariants for exceptional registry metadata corrections."""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
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
LEGACY_MUTABLE_TOP_LEVEL = {
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

# These versions were registered before correction review inheritance became
# mandatory. The closed list is the migration boundary: evidence shape alone
# cannot grandfather a future record, because a new pull request could choose
# the old shape to recover the very re-review path this validator removes.
LEGACY_REREVIEWED_CORRECTIONS = frozenset({
    "entries/PALOMAR-2026-08-20-000006-v2.json",
    "entries/PALOMAR-2026-08-21-000002-v2.json",
    "entries/PALOMAR-2026-08-21-000007-v2.json",
    "entries/PALOMAR-2026-08-21-000008-v2.json",
    "entries/PALOMAR-2026-08-21-000010-v2.json",
    "entries/PALOMAR-2026-08-21-000011-v2.json",
    "entries/PALOMAR-2026-08-23-000002-v2.json",
    "entries/PALOMAR-2026-08-24-000002-v2.json",
    "entries/PALOMAR-2026-08-26-000003-v2.json",
    "entries/PALOMAR-2026-08-26-000005-v3.json",
    "entries/PALOMAR-2026-08-27-000018-v2.json",
    "entries/PALOMAR-2026-08-29-000014-v2.json",
})

CORRECTION_DECISION_KEYS = frozenset({
    "schema_version",
    "kind",
    "submission_id",
    "source",
    "mechanical_report",
    "policy_commit",
    "decided_at",
    "outcome",
    "summary",
    "based_on",
    "changed_fields",
    "inherited_review",
    "inherited_scores",
})
CORRECTION_DECISION_SUMMARY = (
    "The proposed registry metadata correction passed mechanical validation. "
    "No automated editorial review was run; the active baseline review and "
    "its private scores will be inherited unchanged."
)
SHA_RE = re.compile(r"[0-9a-f]{40}")
TIMESTAMP_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
)


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


def correction_errors(
    root: pathlib.Path,
    name: str,
    entry: Mapping[str, Any],
    *,
    require_review_inheritance: bool = False,
) -> list[str]:
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

    mutable_top_level = (
        LEGACY_MUTABLE_TOP_LEVEL - {"review"}
        if require_review_inheritance
        else LEGACY_MUTABLE_TOP_LEVEL
    )
    for key in sorted(set(entry) | set(baseline)):
        if key not in mutable_top_level and entry.get(key) != baseline.get(key):
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
    errors.extend(
        _evidence_errors(
            root,
            name,
            entry,
            correction,
            baseline=baseline,
            require_review_inheritance=require_review_inheritance,
        )
    )
    return errors


def _evidence_errors(
    root: pathlib.Path,
    name: str,
    entry: Mapping[str, Any],
    correction: Mapping[str, Any],
    *,
    baseline: Mapping[str, Any],
    require_review_inheritance: bool,
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
    legacy_names = {
        "baseline-reference.json", "correction-report.json", "workflow-run.json",
        "review.json", "evidence-manifest.json",
    }
    inherited_names = {
        "baseline-reference.json", "correction-report.json", "workflow-run.json",
        "correction-decision.json", "evidence-manifest.json",
    }
    paths = list(bundle.iterdir())
    if any(path.is_symlink() or not stat.S_ISREG(path.stat().st_mode) for path in paths):
        errors.append(f"{name}:registry correction evidence must contain ordinary files only")
        return errors
    names = {path.name for path in paths}
    expected_names = inherited_names if require_review_inheritance else legacy_names
    if names != expected_names:
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
    manifest_version = 3 if require_review_inheritance else 2
    if calculated != tree_hash or manifest != {
        "schema_version": manifest_version, "evidence_tree_sha256": calculated, "files": files
    }:
        errors.append(f"{name}:registry correction evidence manifest or tree hash disagrees")
    based_on = correction.get("based_on")
    if not isinstance(based_on, dict) or baseline_reference != {
        "schema_version": 1,
        "id": identifier,
        "version": based_on.get("version") if isinstance(based_on, dict) else None,
        "path": based_on.get("path") if isinstance(based_on, dict) else None,
        "sha256": based_on.get("sha256") if isinstance(based_on, dict) else None,
        "inherited": (
            [
                "source", "formalization", "verification", "challenge_render",
                "preservation", "trust", "review", "scores",
            ]
            if require_review_inheritance
            else [
                "source", "formalization", "verification", "challenge_render",
                "preservation", "trust",
            ]
        ),
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
    if require_review_inheritance:
        score_path = f"scores/{identifier}-v{based_on.get('version')}.json"
        try:
            score_sha256 = hashlib.sha256((root / score_path).read_bytes()).hexdigest()
        except OSError:
            score_sha256 = None
        try:
            decision = json.loads((bundle / "correction-decision.json").read_bytes())
        except (OSError, json.JSONDecodeError):
            errors.append(f"{name}:correction-decision.json is invalid JSON")
        else:
            expected_source = {
                key: _mapping(entry.get("source")).get(key)
                for key in ("repository", "commit")
            }
            expected_submission_id = _mapping(entry.get("submission")).get(
                "submission_id"
            )
            if (
                not isinstance(decision, dict)
                or set(decision) != CORRECTION_DECISION_KEYS
                or decision.get("schema_version") != 1
                or decision.get("kind") != "registry-metadata-correction"
                or decision.get("outcome") != "neutral"
                or decision.get("submission_id") != expected_submission_id
                or decision.get("source") != expected_source
                or decision.get("mechanical_report") != report.get("workflow_url")
                or not isinstance(decision.get("policy_commit"), str)
                or SHA_RE.fullmatch(decision["policy_commit"]) is None
                or not isinstance(decision.get("decided_at"), str)
                or TIMESTAMP_RE.fullmatch(decision["decided_at"]) is None
                or decision.get("summary") != CORRECTION_DECISION_SUMMARY
                or decision.get("based_on") != {
                    "id": identifier,
                    "version": based_on.get("version"),
                    "path": based_on.get("path"),
                    "sha256": based_on.get("sha256"),
                }
                or decision.get("changed_fields") != correction.get("changed_fields")
                or decision.get("inherited_review") != baseline.get("review")
                or decision.get("inherited_scores") != {
                    "path": score_path,
                    "sha256": score_sha256,
                }
            ):
                errors.append(
                    f"{name}:correction-decision.json does not bind the inherited review"
                )
    return errors
