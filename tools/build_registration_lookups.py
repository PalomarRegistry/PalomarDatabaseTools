#!/usr/bin/env python3
"""Build the bounded public lookups used by the submission form.

The canonical registration projections are organized by Palomar id.  Intake
asks the inverse questions: which registrations belong to this repository,
and does this exact repository/path identity already have one?  These derived
documents answer those questions without publishing a whole-registry index.

Incremental releases only add registrations that gain their first active
version. Takedowns and restorations must remain full, listing-reconciled
rebuilds so repository and identity documents that become empty are removed.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

import patch_surfaces

SCHEMA_VERSION = 2
IDENTITY_SCHEMA_VERSION = 2
REPOSITORY_LIMIT = 500
REPOSITORY_PREFIX = "repositories"
IDENTITY_PREFIX = "registration-identities"
PALOMAR_ID_RE = re.compile(r"PALOMAR-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
REPOSITORY_RE = re.compile(r"[a-z0-9_.-]+/[a-z0-9_.-]+")
IDENTITY_KEYS = {
    "source_repository",
    "project_path",
    "comparator_config_path",
}
REGISTRATION_KEYS = {"id", "project_path", "comparator_config_path"}


def _identity(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) != IDENTITY_KEYS:
        raise ValueError("registration identity has an unsupported shape")
    repository = value.get("source_repository")
    project_path = value.get("project_path")
    comparator_path = value.get("comparator_config_path")
    repository_parts = repository.split("/") if isinstance(repository, str) else []
    if (
        not isinstance(repository, str)
        or REPOSITORY_RE.fullmatch(repository) is None
        or repository != repository.casefold()
        or any(part in {".", ".."} for part in repository_parts)
        or (project_path is not None and not isinstance(project_path, str))
        or not isinstance(comparator_path, str)
        or not comparator_path
    ):
        raise ValueError("registration identity is malformed")
    return {
        "source_repository": repository,
        "project_path": project_path,
        "comparator_config_path": comparator_path,
    }


def identity_digest(identity: Mapping[str, Any]) -> str:
    """SHA-256 of the unambiguous, documented identity preimage."""
    normalized = _identity(identity)
    preimage = "\0".join(
        (
            normalized["source_repository"],
            normalized["project_path"] or "",
            normalized["comparator_config_path"],
        )
    ).encode("utf-8")
    return hashlib.sha256(preimage).hexdigest()


def identity_path(identity: Mapping[str, Any]) -> str:
    return f"{IDENTITY_PREFIX}/{identity_digest(identity)}.json"


def repository_path(repository: str) -> str:
    normalized = repository.casefold()
    if (
        normalized != repository
        or REPOSITORY_RE.fullmatch(normalized) is None
        or any(part in {".", ".."} for part in normalized.split("/"))
    ):
        raise ValueError("registration repository is malformed")
    owner, name = normalized.split("/", 1)
    return f"{REPOSITORY_PREFIX}/{owner}/{name}.json"


def _registration(identifier: object, identity: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _identity(identity)
    if not isinstance(identifier, str) or PALOMAR_ID_RE.fullmatch(identifier) is None:
        raise ValueError("registration id is malformed")
    return {
        "id": identifier,
        "project_path": normalized["project_path"],
        "comparator_config_path": normalized["comparator_config_path"],
    }


def _commit(value: object) -> str:
    if not isinstance(value, str) or COMMIT_RE.fullmatch(value) is None:
        raise ValueError("registration commit is malformed")
    return value


def _active_commits(
    entries: Iterable[Mapping[str, Any]],
) -> tuple[set[str], dict[str, set[str]]]:
    active_ids: set[str] = set()
    commits: dict[str, set[str]] = defaultdict(set)
    for entry in entries:
        identifier = entry.get("id")
        source = entry.get("source")
        if (
            not isinstance(identifier, str)
            or PALOMAR_ID_RE.fullmatch(identifier) is None
            or not isinstance(source, Mapping)
        ):
            raise ValueError("active registration entry is malformed")
        try:
            commit = _commit(source.get("commit"))
        except ValueError as error:
            raise ValueError(f"{identifier}: {error}") from error
        active_ids.add(identifier)
        commits[identifier].add(commit)
    return active_ids, commits


def _write(output: pathlib.Path, relative: str, document: Mapping[str, Any]) -> None:
    target = output / relative
    if not target.resolve().is_relative_to(output.resolve()):
        raise ValueError(f"refusing to write a lookup outside its output: {relative}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def build(
    output: pathlib.Path,
    results: Iterable[Mapping[str, Any]],
    active_entries: Iterable[Mapping[str, Any]],
    historical_entries: Iterable[Mapping[str, Any]],
) -> None:
    """Write complete lookup surfaces for the active registration set."""
    active_ids, _active_commits_by_id = _active_commits(active_entries)
    _historical_ids, commits_by_id = _active_commits(historical_entries)
    by_repository: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_identity: dict[str, tuple[dict[str, Any], list[str]]] = {}
    for result in results:
        identifier = result.get("id")
        if identifier not in active_ids:
            continue
        identity_value = result.get("identity")
        if not isinstance(identity_value, Mapping):
            raise ValueError(f"{identifier}: registration identity is malformed")
        try:
            identity = _identity(identity_value)
            registration = _registration(identifier, identity)
        except ValueError as error:
            raise ValueError(f"{identifier}: {error}") from error
        repository = identity["source_repository"]
        by_repository[repository].append(registration)
        digest = identity_digest(identity)
        stored = by_identity.setdefault(digest, (identity, []))
        if stored[0] != identity:
            raise ValueError("registration identity digest collision")
        stored[1].append(str(identifier))

    for repository, registrations in sorted(by_repository.items()):
        ordered = sorted(registrations, key=lambda row: str(row["id"]), reverse=True)
        _write(
            output,
            repository_path(repository),
            {
                "schema_version": SCHEMA_VERSION,
                "repository": repository,
                "registrations_total": len(ordered),
                "truncated": len(ordered) > REPOSITORY_LIMIT,
                "registrations": ordered[:REPOSITORY_LIMIT],
            },
        )

    for digest, (identity, identifiers) in sorted(by_identity.items()):
        unique = sorted(set(identifiers), reverse=True)
        _write(
            output,
            f"{IDENTITY_PREFIX}/{digest}.json",
            {
                "schema_version": IDENTITY_SCHEMA_VERSION,
                "identity": identity,
                "registration_id": unique[0] if len(unique) == 1 else None,
                "ambiguous": len(unique) > 1,
                "commits": sorted(
                    {
                        commit
                        for identifier in unique
                        for commit in commits_by_id[identifier]
                    }
                ),
            },
        )


def _read_prior(prior: pathlib.Path, relative: str) -> dict[str, Any] | None:
    path = prior / relative
    if path.is_symlink():
        raise patch_surfaces.Rebuild(
            f"the release being served has a symbolic {relative}"
        )
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise patch_surfaces.Rebuild(
            f"the release being served has an invalid {relative}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise patch_surfaces.Rebuild(
            f"the release being served has an invalid {relative}"
        )
    return value


def _prior_repository(
    prior: pathlib.Path, repository: str
) -> tuple[int, list[dict[str, Any]]]:
    relative = repository_path(repository)
    document = _read_prior(prior, relative)
    if document is None:
        return 0, []
    if (
        set(document)
        != {
            "schema_version",
            "repository",
            "registrations_total",
            "truncated",
            "registrations",
        }
        or document.get("schema_version") != SCHEMA_VERSION
        or document.get("repository") != repository
        or type(document.get("registrations_total")) is not int
        or document["registrations_total"] < 0
        or type(document.get("truncated")) is not bool
        or not isinstance(document.get("registrations"), list)
    ):
        raise patch_surfaces.Rebuild(
            f"the release being served has an invalid {relative}"
        )
    rows: list[dict[str, Any]] = []
    previous = None
    for position, row in enumerate(document["registrations"]):
        if not isinstance(row, dict) or set(row) != REGISTRATION_KEYS:
            raise patch_surfaces.Rebuild(
                f"the release being served has a malformed {relative} registrations[{position}]"
            )
        try:
            normalized = _registration(
                row.get("id"),
                {
                    "source_repository": repository,
                    "project_path": row.get("project_path"),
                    "comparator_config_path": row.get("comparator_config_path"),
                },
            )
        except ValueError as error:
            raise patch_surfaces.Rebuild(
                f"the release being served has a malformed {relative} registrations[{position}]"
            ) from error
        identifier = str(normalized["id"])
        if previous is not None and identifier >= previous:
            raise patch_surfaces.Rebuild(
                f"the release being served has an unsorted {relative}"
            )
        previous = identifier
        rows.append(normalized)
    total = document["registrations_total"]
    if (
        len(rows) > REPOSITORY_LIMIT
        or total < len(rows)
        or (
            document["truncated"]
            and (len(rows) != REPOSITORY_LIMIT or total <= REPOSITORY_LIMIT)
        )
        or (not document["truncated"] and total != len(rows))
    ):
        raise patch_surfaces.Rebuild(
            f"the release being served has an inconsistent {relative}"
        )
    return total, rows


def _prior_identity(
    prior: pathlib.Path, identity: Mapping[str, Any]
) -> tuple[str | None, bool, list[str]]:
    normalized = _identity(identity)
    relative = identity_path(normalized)
    document = _read_prior(prior, relative)
    if document is None:
        return None, False, []
    registration_id = document.get("registration_id")
    ambiguous = document.get("ambiguous")
    commits = document.get("commits")
    if (
        set(document)
        != {"schema_version", "identity", "registration_id", "ambiguous", "commits"}
        or document.get("schema_version") != IDENTITY_SCHEMA_VERSION
        or document.get("identity") != normalized
        or type(ambiguous) is not bool
        or not isinstance(commits, list)
        or commits != sorted(set(commits))
        or any(
            not isinstance(commit, str) or COMMIT_RE.fullmatch(commit) is None
            for commit in commits
        )
        or (
            registration_id is not None
            and (
                not isinstance(registration_id, str)
                or PALOMAR_ID_RE.fullmatch(registration_id) is None
            )
        )
        or (ambiguous and registration_id is not None)
        or (not ambiguous and registration_id is None)
    ):
        raise patch_surfaces.Rebuild(
            f"the release being served has an invalid {relative}"
        )
    return registration_id, ambiguous, commits


def patch(
    output: pathlib.Path,
    prior: pathlib.Path,
    additions: Iterable[tuple[str, Mapping[str, Any]]],
    commit_updates: Iterable[tuple[str, Mapping[str, Any], str]],
) -> None:
    """Patch membership for new registrations and commits for every new version."""
    by_repository: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_identity: dict[str, tuple[dict[str, Any], list[str]]] = {}
    for identifier, identity_value in additions:
        identity = _identity(identity_value)
        registration = _registration(identifier, identity)
        by_repository[identity["source_repository"]].append(registration)
        digest = identity_digest(identity)
        stored = by_identity.setdefault(digest, (identity, []))
        if stored[0] != identity:
            raise ValueError("registration identity digest collision")
        stored[1].append(identifier)

    commits_by_identity: dict[
        str, tuple[dict[str, Any], list[tuple[str, str]]]
    ] = {}
    for identifier, identity_value, commit_value in commit_updates:
        identity = _identity(identity_value)
        if PALOMAR_ID_RE.fullmatch(identifier) is None:
            raise ValueError("registration id is malformed")
        commit = _commit(commit_value)
        digest = identity_digest(identity)
        stored = commits_by_identity.setdefault(digest, (identity, []))
        if stored[0] != identity:
            raise ValueError("registration identity digest collision")
        stored[1].append((identifier, commit))

    for repository, registrations in sorted(by_repository.items()):
        total, current = _prior_repository(prior, repository)
        identifiers = {str(row["id"]) for row in current}
        for row in registrations:
            if row["id"] in identifiers:
                raise patch_surfaces.Rebuild(
                    f"the release being served already contains {row['id']} in "
                    f"{repository_path(repository)}"
                )
            identifiers.add(str(row["id"]))
            current.append(row)
        current.sort(key=lambda row: str(row["id"]), reverse=True)
        total += len(registrations)
        _write(
            output,
            repository_path(repository),
            {
                "schema_version": SCHEMA_VERSION,
                "repository": repository,
                "registrations_total": total,
                "truncated": total > REPOSITORY_LIMIT,
                "registrations": current[:REPOSITORY_LIMIT],
            },
        )

    for digest in sorted(set(by_identity) | set(commits_by_identity)):
        if digest in by_identity:
            identity, identifiers = by_identity[digest]
        else:
            identity, _commit_updates = commits_by_identity[digest]
            identifiers = []
        registration_id, ambiguous, prior_commits = _prior_identity(prior, identity)
        unique_new = sorted(set(identifiers), reverse=True)
        updates = commits_by_identity.get(digest, (identity, []))[1]
        updated_ids = {identifier for identifier, _commit_value in updates}
        if len(unique_new) != len(identifiers):
            raise ValueError("one release repeats a registration id")
        if not set(unique_new) <= updated_ids:
            raise ValueError("a newly active registration has no source commit")
        if not unique_new:
            if registration_id is None and not ambiguous:
                raise patch_surfaces.Rebuild(
                    f"the release being served is missing {IDENTITY_PREFIX}/{digest}.json"
                )
            if registration_id is not None and updated_ids != {registration_id}:
                raise patch_surfaces.Rebuild(
                    f"the release commit update disagrees with {IDENTITY_PREFIX}/{digest}.json"
                )
            next_id = registration_id
            next_ambiguous = ambiguous
        elif ambiguous:
            next_id = None
            next_ambiguous = True
        elif registration_id is None and len(unique_new) == 1:
            next_id = unique_new[0]
            next_ambiguous = False
        else:
            if registration_id in unique_new:
                raise patch_surfaces.Rebuild(
                    f"the release being served already contains {registration_id} in "
                    f"{IDENTITY_PREFIX}/{digest}.json"
                )
            next_id = None
            next_ambiguous = True
        _write(
            output,
            f"{IDENTITY_PREFIX}/{digest}.json",
            {
                "schema_version": IDENTITY_SCHEMA_VERSION,
                "identity": identity,
                "registration_id": next_id,
                "ambiguous": next_ambiguous,
                "commits": sorted(
                    set(prior_commits)
                    | {commit for _identifier, commit in updates}
                ),
            },
        )
