"""Classify and authorize the one bounded database-validation transition.

The complete validator owns record semantics.  This module owns the narrower
question that must be settled before those semantics may be applied only to an
accepted delta: which committed paths arrived, whether every induction premise
is unchanged, and whether publication's served parent authenticates that exact
base.  Every uncertainty returns ``None`` and therefore requests the complete
path.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
from dataclasses import dataclass

import changed_records
import registration_projection
import release_delta
import score_validation
from entry_validation import ENTRY_SCHEMA_NAMES, PALOMAR_ID_RE
from takedowns import committed_manifest_blob


ENTRY_PATH_RE = re.compile(
    rf"entries/{PALOMAR_ID_RE.pattern}-v[1-9][0-9]*\.json"
)

# Changing any of these changes what a *previously accepted* record means, so a
# scoped run would be checking new records against rules that have moved under
# the old ones. The orchestrator and extracted domain policies are global and
# therefore force a complete run.
GLOBAL_POLICY_PATHS = (
    "tools/bundle_reference_validation.py",
    "tools/correction_validation.py",
    "tools/entry_validation.py",
    "tools/evidence_validation.py",
    "tools/render_validation.py",
    "tools/registration_projection.py",
    "tools/score_validation.py",
    "tools/schema_policy.py",
    "tools/validation_scope.py",
    "tools/validate.py",
    "tools/takedowns.py",
    "takedowns.json",
)
FROZEN_DIRECTORIES = (
    "entries",
    "renders",
    "evidence",
    "scores",
    registration_projection.SUBMISSIONS_DIRECTORY,
)


@dataclass(frozen=True)
class ValidationScope:
    """The append-only part of one change.

    ``frozen_paths`` is the complete set of ordinary file additions under the
    four append-only trees, not merely the entries that happen to name them;
    that is what lets validation reject an orphan render, evidence file, or
    score without walking historical directories.
    """

    entries: frozenset[str]
    frozen_paths: frozenset[str]
    base: str
    registration_paths: frozenset[str]


def scope_of(root: pathlib.Path, base: str) -> ValidationScope | None:
    """Which bundle additions a run may confine hashing to, or None for all.

    `None` means "check everything", and it is the answer to every uncertainty:
    an unreadable diff, a schema in use, a changed allowlist, a changed
    validator. Falling back is always correct and merely slow, so it is the
    default branch and not the exception.

    The diff is computed and judged *here* rather than taken from the sibling
    append-only job. The base commit's records and projections are an explicit
    induction premise: main already passed full validation there, and the
    scheduled sweep rechecks that premise. This function independently proves
    that the current change is only the closed append transition for which the
    premise is sound.
    """
    try:
        changed = changed_records.changed_since(root, base)
    except changed_records.CannotTell as reason:
        print(f"validating everything: {reason}")
        return None

    for path in sorted(changed):
        if path in GLOBAL_POLICY_PATHS or path in (
            *ENTRY_SCHEMA_NAMES.values(),
            score_validation.SCORES_SCHEMA_NAME,
        ):
            print(f"validating everything: {path} decides what an existing record means")
            return None
    try:
        changed_records.require_clean(root, ("registrations",))
        additions = changed_records.ordinary_additions_since(
            root, base, FROZEN_DIRECTORIES
        )
    except changed_records.CannotTell as reason:
        print(f"validating everything: {reason}")
        return None
    frozen_prefixes = tuple(f"{name}/" for name in FROZEN_DIRECTORIES)
    frozen_changed = {path for path in changed if path.startswith(frozen_prefixes)}
    if frozen_changed != additions:
        # This normally means uncommitted work. The committed diff parser above
        # is deliberately the authority on modes and additions, so a path it
        # did not attest cannot enter the cheap path.
        print("validating everything: the append-only change has no complete committed scope")
        return None
    for path in sorted(additions):
        if path.startswith("entries/") and not ENTRY_PATH_RE.fullmatch(path):
            print(f"validating everything: {path} is not a canonical entry path")
            return None
        if path.startswith("scores/") and not score_validation.SCORE_PATH_RE.fullmatch(path):
            print(f"validating everything: {path} is not a canonical scores path")
            return None
    entries = frozenset(path for path in additions if path.startswith("entries/"))
    entry_stems = {
        pathlib.PurePosixPath(relative).stem for relative in entries
    }
    for directory in ("renders", "evidence"):
        for bundle_root in changed_records.changed_bundle_roots(
            frozenset(additions), directory
        ):
            parts = bundle_root.split("/")
            if len(parts) < 2 or parts[1] not in entry_stems:
                print(
                    f"validating everything: {bundle_root} changes a historical bundle"
                )
                return None
    registration_paths = {
        path for path in changed if path.startswith("registrations/")
    }
    if any(
        registration_projection.RESULT_PATH_RE.fullmatch(path) is None
        and registration_projection.SUBMISSION_PATH_RE.fullmatch(path) is None
        and registration_projection.DAY_PATH_RE.fullmatch(path) is None
        and registration_projection.IDENTITY_PATH_RE.fullmatch(path) is None
        for path in registration_paths
    ):
        print("validating everything: a registration projection has a noncanonical path")
        return None
    unexpected = {
        path
        for path in changed
        if not path.startswith(
            ("entries/", "renders/", "evidence/", "scores/", "registrations/")
        )
    }
    if unexpected:
        # Something outside the record paths moved. It may be harmless, but
        # deciding which is the sort of judgement that goes stale, so the
        # answer is to check all of it.
        print(f"validating everything: {sorted(unexpected)[0]} is outside the record paths")
        return None
    return ValidationScope(entries, frozenset(additions), base, frozenset(registration_paths))


def sparse_checkout_paths(scope: ValidationScope) -> tuple[str, ...]:
    """Current blobs a scoped validation/publication is allowed to materialize.

    Policy, schemas, tooling and workflow inputs are the checkout's fixed sparse
    base. Everything dependent on an accepted event is either an attested
    immutable addition or one of its exact segmented projection transitions.
    Existing takedowns are unchanged by definition and are bound by their Git
    blob identity without materializing the growing policy document.
    NUL-delimited transport keeps every Git path unambiguous until the shell
    checks its tree metadata and checks it out by index entry.
    """
    return tuple(sorted(scope.frozen_paths | scope.registration_paths))


def sparse_dependency_paths(
    root: pathlib.Path, scope: ValidationScope
) -> tuple[str, ...]:
    """Unchanged current blobs needed to validate one accepted delta.

    The first sparse phase materializes only attested additions and changed
    projections.  Once those bytes are present, a correction can name its
    immutable predecessor and an appended version's result projection can
    name the unchanged identity authority that must be read at the validated
    base.  Resolve only those two closed, canonical dependency classes; every
    semantic check remains the complete validator's job.
    """
    dependencies: set[str] = set()
    for relative in sorted(scope.entries):
        try:
            entry = json.loads((root / relative).read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        correction = entry.get("registry_correction") if isinstance(entry, dict) else None
        based_on = correction.get("based_on") if isinstance(correction, dict) else None
        path = based_on.get("path") if isinstance(based_on, dict) else None
        if isinstance(path, str) and ENTRY_PATH_RE.fullmatch(path):
            dependencies.add(path)

    for relative in sorted(scope.registration_paths):
        if registration_projection.RESULT_PATH_RE.fullmatch(relative) is None:
            continue
        try:
            result = json.loads((root / relative).read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        identity = result.get("identity") if isinstance(result, dict) else None
        if not isinstance(identity, dict) or set(identity) != {
            "source_repository", "project_path", "comparator_config_path"
        }:
            continue
        try:
            dependencies.add(registration_projection.identity_path(identity))
        except (KeyError, TypeError):
            continue

    return tuple(sorted(dependencies - scope.frozen_paths - scope.registration_paths))


def scoped_parent_errors(
    root: pathlib.Path,
    scope: ValidationScope,
    previous_base: pathlib.Path,
) -> list[str]:
    """Bind a publication scope to its complete authenticated served parent."""
    try:
        raw = previous_base.read_bytes()
        previous = release_delta.parse_base(raw)
        if raw != release_delta.canonical_base_bytes(previous):
            raise ValueError("publication base is not in canonical byte form")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        return [f"served publication base cannot authorize scoped publication: {error}"]
    resolved = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", f"{scope.base}^{{commit}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if resolved.returncode:
        return ["served release validation base cannot be resolved"]
    if previous["database_commit"] != resolved.stdout.strip():
        return ["served publication base names a different validation base"]
    try:
        current_blob = committed_manifest_blob(root, "HEAD")
        base_blob = committed_manifest_blob(root, scope.base)
    except ValueError as error:
        return [str(error)]
    if (
        current_blob != base_blob
        or current_blob != previous["takedowns_git_blob"]
    ):
        return ["committed takedown authority disagrees with the served publication base"]
    return []
