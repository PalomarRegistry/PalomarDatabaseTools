#!/usr/bin/env python3
"""Parse and validate the private, reversible takedown manifest."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import re
import subprocess
from collections.abc import Iterable
from typing import Any

PALOMAR_ID_RE = re.compile(
    r"PALOMAR-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}"
)
UTC_TIMESTAMP_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
)
# Deliberately unchanged while the row schema below changed under it. No row
# has ever existed: the manifest has been an empty list since it was
# introduced, so there is nothing this number could distinguish. Bumping it
# would also have to be a two-step change, because `validate.yml` judges a pull
# request with the validator from its base revision, and that validator rejects
# a version it has not been taught yet.
SCHEMA_VERSION = 1
MAX_REASON_LENGTH = 4_000
TOP_LEVEL_KEYS = frozenset({"schema_version", "takedowns"})
ROW_KEYS = frozenset(
    {
        "id",
        "version",
        "taken_down_at",
        "authorized_by_login",
        "authorization_issue",
        "reason",
    }
)
# Which GitHub account is which Moderator on Palomar Policy's governance
# roster. Authority is bound to the login, because that is what a GitHub event
# authenticates; the name beside it is only how the binding is read back to a
# person. All Moderators and Technical Maintainers are trusted, so this is
# identity binding and not an attempt to constrain anybody on it.
#
# A Moderator whose login is not confirmed here appears in
# `MODERATORS_WITHOUT_A_BOUND_LOGIN` instead and cannot drive the moderation
# workflow: an unconfirmed guess would bind Palomar's most consequential
# action to whichever account happens to hold that name. Add the login here
# once it has been confirmed with the person, and remove the name below.
MODERATOR_LOGINS: dict[str, str] = {
    "avigad": "Jeremy Avigad",
    "kim-em": "Kim Morrison",
    "mattrobball": "Matthew Ballard",
    "teorth": "Terence Tao",
}
MODERATORS_WITHOUT_A_BOUND_LOGIN = frozenset(
    {
        "Jaume de Dios",
        "Nestor Guillen",
        "Bryna Kra",
        "Ravi Vakil",
        "Akshay Venkatesh",
    }
)
MODERATORS = frozenset(MODERATOR_LOGINS.values()) | MODERATORS_WITHOUT_A_BOUND_LOGIN
GIT_BLOB_RE = re.compile(rb"100644 blob ([0-9a-f]{40})\ttakedowns\.json\x00")


def committed_manifest_blob(root: pathlib.Path, revision: str) -> str:
    """The exact regular Git blob that is the takedown authority at a revision.

    Scoped publication deliberately does not materialize or enumerate the
    potentially large manifest.  Its authenticated parent release binds this
    constant-size tree identity instead.  Anything other than one ordinary,
    non-executable blob is uncertainty and therefore cannot authorize the
    narrow path.
    """
    result = subprocess.run(
        ["git", "-C", str(root), "ls-tree", "-z", revision, "--", "takedowns.json"],
        check=False,
        capture_output=True,
    )
    if result.returncode:
        raise ValueError(f"cannot resolve the committed takedown authority at {revision}")
    match = GIT_BLOB_RE.fullmatch(result.stdout)
    if match is None:
        raise ValueError(
            f"the committed takedown authority at {revision} is missing or not a 100644 blob"
        )
    return match.group(1).decode("ascii")


def manifest_blob(root: pathlib.Path) -> str:
    """Git-blob identity of a complete checked-out manifest.

    Full/offline staging may intentionally run outside a Git worktree and has
    already paid to read and validate the complete policy.  Computing Git's
    ordinary blob identity locally preserves the release contract without
    pretending that the worktree was a committed scoped authority.
    """
    path = root / "takedowns.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError("takedowns.json is missing or symbolic")
    raw = path.read_bytes()
    return hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()


def moderator_login(value: object) -> str | None:
    """The canonical roster login for `value`, or None if it is not a Moderator.

    GitHub compares logins without regard to case, so a request that arrives
    as `Avigad` names the same account as `avigad`. The manifest records the
    canonical spelling; everything else fails closed.
    """
    if not isinstance(value, str):
        return None
    folded = value.strip().casefold()
    for login in MODERATOR_LOGINS:
        if login.casefold() == folded:
            return login
    return None


def _valid_utc_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not UTC_TIMESTAMP_RE.fullmatch(value):
        return False
    try:
        dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def validate_takedowns(
    value: object,
    entry_targets: Iterable[tuple[str, int]],
) -> list[str]:
    """Return every closed-schema or referential-integrity error."""
    errors: list[str] = []
    targets = set(entry_targets)
    if not isinstance(value, dict):
        return ["takedowns.json: must be a JSON object"]
    if set(value) != TOP_LEVEL_KEYS:
        errors.append(
            "takedowns.json: must contain exactly schema_version and takedowns"
        )
    if value.get("schema_version") != SCHEMA_VERSION:
        errors.append("takedowns.json: unsupported schema_version")
    rows = value.get("takedowns")
    if not isinstance(rows, list):
        errors.append("takedowns.json: takedowns must be an array")
        return errors

    ordered_targets: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    issues: set[int] = set()
    for position, row in enumerate(rows):
        prefix = f"takedowns.json:takedowns.{position}"
        if not isinstance(row, dict):
            errors.append(f"{prefix}: must be an object")
            continue
        if set(row) != ROW_KEYS:
            errors.append(
                f"{prefix}: must contain exactly id, version, taken_down_at, "
                "authorized_by_login, authorization_issue, and reason"
            )
        identifier = row.get("id")
        version = row.get("version")
        if not isinstance(identifier, str) or not PALOMAR_ID_RE.fullmatch(identifier):
            errors.append(f"{prefix}.id: must be a canonical Palomar identifier")
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version < 1
        ):
            errors.append(f"{prefix}.version: must be a positive integer")
        if not _valid_utc_timestamp(row.get("taken_down_at")):
            errors.append(
                f"{prefix}.taken_down_at: must be a real UTC timestamp to the second"
            )
        authorized_by_login = row.get("authorized_by_login")
        if (
            not isinstance(authorized_by_login, str)
            or moderator_login(authorized_by_login) != authorized_by_login
        ):
            errors.append(
                f"{prefix}.authorized_by_login: must be a current Palomar "
                "Moderator's GitHub login, spelled as this repository records it"
            )
        authorization_issue = row.get("authorization_issue")
        if (
            not isinstance(authorization_issue, int)
            or isinstance(authorization_issue, bool)
            or authorization_issue < 1
        ):
            errors.append(
                f"{prefix}.authorization_issue: must be the positive number of the "
                "private issue this repository holds for this action"
            )
        else:
            if authorization_issue in issues:
                errors.append(
                    f"{prefix}.authorization_issue: authorizes more than one row"
                )
            issues.add(authorization_issue)
        reason = row.get("reason")
        if not isinstance(reason, str) or not 1 <= len(reason) <= MAX_REASON_LENGTH:
            errors.append(
                f"{prefix}.reason: must contain 1 to {MAX_REASON_LENGTH} characters"
            )
        if isinstance(identifier, str) and isinstance(version, int) and not isinstance(version, bool):
            target = (identifier, version)
            ordered_targets.append(target)
            if target in seen:
                errors.append(f"{prefix}: target is duplicated")
            seen.add(target)
            if target not in targets:
                errors.append(f"{prefix}: target does not exist in entries/")
    if ordered_targets != sorted(ordered_targets):
        errors.append("takedowns.json: rows must be sorted by id and version")
    return errors


def _targets_on_disk(root: pathlib.Path, rows: object) -> set[tuple[str, int]]:
    """Which of the targets this manifest names are records in the database.

    Asking the filesystem about the rows, rather than being handed every
    record the database holds, is what lets a caller check a takedown without
    first reading the whole registry: the manifest is bounded by the number of
    withdrawals, and each row names exactly one path.
    """
    found: set[tuple[str, int]] = set()
    if not isinstance(rows, list):
        return found
    for row in rows:
        if not isinstance(row, dict):
            continue
        identifier, version = row.get("id"), row.get("version")
        if not isinstance(identifier, str) or not PALOMAR_ID_RE.fullmatch(identifier):
            continue
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            continue
        if (root / "entries" / f"{identifier}-v{version}.json").is_file():
            found.add((identifier, version))
    return found


def _read_manifest(root: pathlib.Path) -> tuple[object | None, list[str]]:
    path = root / "takedowns.json"
    if path.is_symlink() or not path.is_file():
        return None, ["takedowns.json: file is missing or symbolic"]
    try:
        return json.loads(path.read_text(encoding="utf-8")), []
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return None, [f"takedowns.json: invalid JSON: {error}"]


def _rows(value: object) -> list[dict[str, Any]]:
    rows = value.get("takedowns", []) if isinstance(value, dict) else []
    return [row for row in rows if isinstance(row, dict)]


def load_takedowns(
    root: pathlib.Path,
    entry_targets: Iterable[tuple[str, int]] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Load the manifest, returning rows and validation errors without raising.

    `entry_targets` is every record the caller already has in hand. Without
    one, the targets this manifest names are looked up on disk instead, which
    costs one stat per withdrawal rather than a pass over the registry.
    """
    value, read_errors = _read_manifest(root)
    if read_errors:
        return [], read_errors
    if entry_targets is None:
        entry_targets = _targets_on_disk(
            root, value.get("takedowns") if isinstance(value, dict) else None
        )
    errors = validate_takedowns(value, entry_targets)
    return _rows(value), errors
