"""Validate one immutable verification-evidence bundle against its entry.

This module owns the evidence filesystem limits, content-addressed manifest,
and bindings from archived review, mechanical report, source receipt, and run
provenance back to the canonical entry. It reads only the supplied database
root and entry and returns errors without mutating either; database traversal
and validation scope remain with validate.py.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import stat
from collections.abc import Mapping
from typing import Any

from entry_validation import (
    PALOMAR_ID_RE,
    VERIFICATION_REPOSITORY,
    WORKFLOW_URL_RE,
)

MAX_EVIDENCE_FILE_BYTES = 16 * 1024 * 1024
MAX_EVIDENCE_BYTES = 24 * 1024 * 1024
EVIDENCE_FILES = frozenset(
    {
        "mechanical-report.json",
        "workflow-run.json",
        "review.json",
        "evidence-manifest.json",
        "source-archive.json",
    }
)
# The two directories are the record-version and content-digest components
# below evidence/. Keeping this derived from the permitted bundle files makes
# the database-wide sweep follow this module's closed filesystem contract.
MAX_EVIDENCE_NODES = len(EVIDENCE_FILES) + 2
VERIFY_WORKFLOW_PATH = ".github/workflows/submission.yml"


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mapping(value: object) -> Mapping[str, Any]:
    """Return a JSON object as a mapping, or an empty mapping otherwise."""
    return value if isinstance(value, dict) else {}


def validate_evidence(
    root: pathlib.Path, entry: dict[str, object], name: str
) -> list[str]:
    """Return bundle errors after the caller has schema-validated the entry.

    An evidence shape rejected by the entry schema returns no errors here
    because there is no safe bundle identity to inspect.
    """
    verification = entry.get("verification")
    if not isinstance(verification, dict):
        return []  # The JSON Schema reports the missing or malformed object.
    evidence_path = verification.get("evidence_path")
    tree_hash = verification.get("evidence_tree_sha256")
    report_hash = verification.get("mechanical_report_sha256")
    identifier = entry.get("id")
    version = entry.get("version")
    if (
        not isinstance(evidence_path, str)
        or not isinstance(tree_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", tree_hash)
        or not isinstance(report_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", report_hash)
        or not isinstance(identifier, str)
        or not PALOMAR_ID_RE.fullmatch(identifier)
        or not isinstance(version, int)
        or isinstance(version, bool)
        or version < 1
    ):
        return []
    expected_path = f"evidence/{identifier}-v{version}/{tree_hash}/"
    errors: list[str] = []
    if evidence_path != expected_path:
        return [f"{name}:verification.evidence_path: must be {expected_path}"]
    bundle = root / evidence_path
    current = root
    for part in pathlib.PurePosixPath(evidence_path).parts:
        current /= part
        if current.is_symlink():
            return [f"{name}:verification evidence path contains a symbolic link"]
    if bundle.is_symlink() or not bundle.is_dir():
        return [f"{name}:verification evidence directory is missing or symbolic"]

    files: list[dict[str, object]] = []
    total_bytes = 0
    for path in sorted(bundle.rglob("*")):
        relative = path.relative_to(bundle).as_posix()
        if path.is_symlink():
            errors.append(f"{name}:verification evidence contains a symbolic link: {relative}")
            continue
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            errors.append(f"{name}:verification evidence contains an unexpected directory: {relative}")
            continue
        if not stat.S_ISREG(mode) or relative not in EVIDENCE_FILES:
            errors.append(f"{name}:verification evidence contains an unexpected file: {relative}")
            continue
        size = path.stat().st_size
        if size > MAX_EVIDENCE_FILE_BYTES:
            errors.append(f"{name}:verification evidence file exceeds the size cap: {relative}")
            continue
        if relative == "evidence-manifest.json":
            continue
        total_bytes += size
        files.append({"path": relative, "bytes": size, "sha256": _sha256(path)})
    if total_bytes > MAX_EVIDENCE_BYTES:
        errors.append(f"{name}:verification evidence exceeds the total-size cap")
    required_files = {
        "mechanical-report.json",
        "workflow-run.json",
        "review.json",
        "source-archive.json",
    }
    if {item["path"] for item in files} != required_files:
        errors.append(
            f"{name}:verification evidence must contain the report, the run provenance, "
            "and the review, plus the source archive receipt"
        )
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    actual_tree_hash = hashlib.sha256(canonical).hexdigest()
    if actual_tree_hash != tree_hash:
        errors.append(
            f"{name}:verification.evidence_tree_sha256: content hashes to {actual_tree_hash}"
        )
    manifest_path = bundle / "evidence-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        errors.append(f"{name}:verification evidence has an invalid manifest: {error}")
    else:
        expected_manifest = {
            "schema_version": 1,
            "evidence_tree_sha256": actual_tree_hash,
            "files": files,
        }
        if manifest != expected_manifest:
            errors.append(f"{name}:verification evidence manifest does not match its files")

    # The archived review is what the record's decision rests on, so the entry
    # must cite the exact bytes that are served.
    #
    # Those are not quite the bytes the submitter read. The archived copy is
    # redacted when it is written: the scores that decided the outcome, and the
    # severity on each finding, are internal and stay in the private submission
    # record. What this check establishes is that the review anyone can read is
    # the one this record was built from. Whether it faithfully redacts the
    # delivered review is not checkable from here, and the unredacted copy is
    # in PalomarSubmissionState for the moderation team to compare.
    review_path = bundle / "review.json"
    review_hash = _mapping(_mapping(entry.get("review")).get("report")).get("sha256")
    if review_path.is_file() and not review_path.is_symlink():
        if _sha256(review_path) != review_hash:
            errors.append(f"{name}:review.report.sha256 does not match the archived review")

    report_path = bundle / "mechanical-report.json"
    if report_path.is_file() and not report_path.is_symlink():
        if _sha256(report_path) != report_hash:
            errors.append(f"{name}:verification.mechanical_report_sha256 does not match the report")
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            errors.append(f"{name}:verification mechanical report is invalid JSON: {error}")
        else:
            if not isinstance(report, dict):
                errors.append(f"{name}:verification mechanical report must be a JSON object")
            else:
                source = _mapping(report.get("source"))
                challenge = _mapping(report.get("challenge"))
                solution = _mapping(report.get("solution"))
                submission_block = _mapping(report.get("submission"))
                bindings = (
                    (report.get("status"), "pass", "status"),
                    (report.get("stage"), "complete", "stage"),
                    (report.get("workflow_url"), verification.get("workflow_url"), "workflow_url"),
                    (report.get("checked_at"), verification.get("verified_at"), "checked_at"),
                    (report.get("comparator_commit"), verification.get("comparator_commit"), "comparator_commit"),
                    (report.get("lean4export_commit"), verification.get("lean4export_commit"), "lean4export_commit"),
                    (report.get("landrun_commit"), verification.get("landrun_commit"), "landrun_commit"),
                    (report.get("nanoda_commit"), verification.get("nanoda_commit"), "nanoda_commit"),
                    (source.get("repository"), _mapping(entry.get("source")).get("repository"), "source.repository"),
                    (source.get("commit"), _mapping(entry.get("source")).get("commit"), "source.commit"),
                    (challenge.get("sha256"), verification.get("challenge_sha256"), "challenge.sha256"),
                    (solution.get("sha256"), verification.get("solution_sha256"), "solution.sha256"),
                    (submission_block.get("submission_id"),
                     _mapping(entry.get("submission")).get("submission_id"),
                     "submission.submission_id"),
                )
                entry_source = _mapping(entry.get("source"))
                formalization = _mapping(entry.get("formalization"))
                report_version = report.get("schema_version")
                if report_version != 1:
                    errors.append(
                        f"{name}:verification requires mechanical report schema 1"
                    )
                else:
                    report_paths = {
                        "source.project_path": source.get("project_path"),
                        "formalization.challenge_path": challenge.get("path"),
                        "formalization.solution_path": solution.get("path"),
                        "formalization.comparator_config_path": _mapping(
                            report.get("comparator")
                        ).get("path"),
                        "formalization.formalization_metadata_path": _mapping(
                            report.get("formalization")
                        ).get("path"),
                        "formalization.lakefile_path": _mapping(report.get("lakefile")).get(
                            "path"
                        ),
                    }
                    entry_paths = {
                        "source.project_path": entry_source.get("project_path"),
                        **{
                            f"formalization.{field}": formalization.get(field)
                            for field in (
                                "challenge_path",
                                "solution_path",
                                "comparator_config_path",
                                "formalization_metadata_path",
                                "lakefile_path",
                            )
                        },
                    }
                    bindings = (
                        *bindings,
                        *(
                            (report_paths[field], entry_paths[field], field)
                            for field in entry_paths
                        ),
                    )
                for actual, expected, field in bindings:
                    if actual != expected:
                        errors.append(f"{name}:verification mechanical report disagrees on {field}")

    archive_path = bundle / "source-archive.json"
    preservation = _mapping(entry.get("preservation"))
    if archive_path.is_file() and not archive_path.is_symlink():
        if _sha256(archive_path) != preservation.get("receipt_sha256"):
            errors.append(f"{name}:preservation.receipt_sha256 does not match the archive receipt")
        try:
            archive_receipt = json.loads(archive_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            errors.append(f"{name}:source archive receipt is invalid JSON: {error}")
        else:
            expected_receipt = {
                "schema_version": 1,
                "id": entry.get("id"),
                "version": entry.get("version"),
                "archive_owner": preservation.get("archive_owner"),
                "archived_at": preservation.get("archived_at"),
                "repositories": preservation.get("repositories"),
            }
            if archive_receipt != expected_receipt:
                errors.append(f"{name}:source archive receipt does not match preservation metadata")

    run_path = bundle / "workflow-run.json"
    if run_path.is_file() and not run_path.is_symlink():
        try:
            provenance = json.loads(run_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            errors.append(f"{name}:verification run provenance is invalid JSON: {error}")
        else:
            run_url = verification.get("workflow_url")
            run_match = WORKFLOW_URL_RE.fullmatch(run_url) if isinstance(run_url, str) else None
            tail = run_url.rsplit("/", 1)[-1] if run_match else ""
            expected_run_id = int(tail) if tail.isdigit() else None
            required = {
                "schema_version", "repository", "run_id", "run_attempt", "workflow_path",
                "workflow_commit", "workflow_url", "event", "status", "conclusion",
                "created_at", "updated_at", "jobs",
            }
            if not isinstance(provenance, dict) or set(provenance) != required:
                errors.append(f"{name}:verification run provenance has unexpected fields")
            else:
                expected_values = {
                    "schema_version": 1,
                    "repository": VERIFICATION_REPOSITORY,
                    "run_id": expected_run_id,
                    "run_attempt": verification.get("workflow_run_attempt"),
                    "workflow_path": VERIFY_WORKFLOW_PATH,
                    "workflow_commit": verification.get("workflow_commit"),
                    "workflow_url": run_url,
                    "event": "workflow_dispatch",
                    "status": "completed",
                    "conclusion": "success",
                }
                for field, expected in expected_values.items():
                    if provenance.get(field) != expected:
                        errors.append(f"{name}:verification run provenance disagrees on {field}")
                for field in ("created_at", "updated_at"):
                    if not isinstance(provenance.get(field), str) or not provenance[field]:
                        errors.append(
                            f"{name}:verification run provenance has invalid {field}"
                        )
                jobs = provenance.get("jobs")
                if not isinstance(jobs, list) or not jobs:
                    errors.append(f"{name}:verification run provenance must record its jobs")
                else:
                    seen_jobs: set[int] = set()
                    for position, job in enumerate(jobs):
                        if not isinstance(job, dict) or set(job) != {
                            "id", "name", "status", "conclusion", "started_at", "completed_at"
                        }:
                            errors.append(
                                f"{name}:verification run job {position} has unexpected fields"
                            )
                            continue
                        job_id = job.get("id")
                        if (
                            not isinstance(job_id, int)
                            or isinstance(job_id, bool)
                            or job_id < 1
                            or job_id in seen_jobs
                            or job.get("status") != "completed"
                            or job.get("conclusion") not in {"success", "skipped"}
                            or not all(
                                isinstance(job.get(field), str) and bool(job[field])
                                for field in ("name", "started_at", "completed_at")
                            )
                        ):
                            errors.append(f"{name}:verification run job {position} is malformed")
                        else:
                            seen_jobs.add(job_id)
    return errors
