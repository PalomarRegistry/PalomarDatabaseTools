"""The sole canonical entry contract and its cross-field invariants.

JSON Schema owns the shape of a record. The checks here own relationships a
schema cannot express: URLs derived from repository identities, safe relative
paths, the exact preservation closure, and trust dependencies. They accept one
decoded entry and return errors without reading or changing database state, so
the boundary can be exercised independently of validation orchestration.

Schema discovery lives beside those semantics because there is exactly one
current entry contract. It is the only filesystem operation in this module.
"""

from __future__ import annotations

import pathlib
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import jsonschema
from referencing import Registry

from schema_policy import (
    InvalidSchemaJSON,
    UnsafeSchemaReferences,
    parse_schema_json,
    validate_closed_schema_references,
)

ENTRY_SCHEMA_VERSION = 3
ENTRY_SCHEMA_NAME = "schema-v3.json"
ENTRY_SCHEMA_EVALUATION_ERROR = (
    f"{ENTRY_SCHEMA_NAME}: entry schema cannot be evaluated safely"
)
PALOMAR_ID_RE = re.compile(
    r"PALOMAR-(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})-(?P<serial>[0-9]{6})"
)
LICENSE_PATH_RE = re.compile(
    r"(?:licen[cs]e|copying|unlicense|ofl)(?:\.(?:md|markdown|txt))?",
    re.IGNORECASE,
)

# Verification identity is fixed by the current entry contract. The run URL is
# therefore derived rather than accepted as an independent assertion.
VERIFICATION_REPOSITORY = "PalomarRegistry/PalomarSubmission"
HIGH_TRUST_CHALLENGE_REPOSITORIES = frozenset(
    {
        "leanprover-community/mathlib4",
    }
)

GITHUB_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
WORKFLOW_URL_RE = re.compile(
    rf"https://github\.com/{re.escape(VERIFICATION_REPOSITORY)}/actions/runs/[1-9][0-9]*\Z"
)

# The verifier canonicalizes this historical repository name before recording
# challenge trust. Project-dependency metadata can still contain the old name.
REPOSITORY_ALIASES = {
    "formalfrontier/tauceti": "taucetiproject/tauceti",
}


class EntrySchemaUnevaluable(Exception):
    """The schema is valid JSON Schema syntax but cannot judge an entry."""


def load_entry_schema(
    root: pathlib.Path,
) -> tuple[jsonschema.Draft202012Validator | None, list[str]]:
    """Load the sole entry schema without an invalid-policy fallback."""
    errors: list[str] = []
    for path in sorted(root.glob("schema-v*.json")):
        if path.name != ENTRY_SCHEMA_NAME:
            errors.append(
                f"{path.name}: unsupported entry schema document; {ENTRY_SCHEMA_NAME} "
                "is the sole current entry contract"
            )

    path = root / ENTRY_SCHEMA_NAME
    if path.is_symlink() or not path.is_file():
        errors.append(f"{ENTRY_SCHEMA_NAME}: the sole entry schema is missing or symbolic")
        return None, errors
    try:
        mode = path.stat().st_mode
    except OSError:
        errors.append(f"{ENTRY_SCHEMA_NAME}: entry schema cannot be read")
        return None, errors
    if mode & 0o111:
        errors.append(
            f"{ENTRY_SCHEMA_NAME}: the sole entry schema must be a non-executable ordinary file"
        )
        return None, errors
    try:
        encoded = path.read_text(encoding="utf-8")
    except OSError:
        errors.append(f"{ENTRY_SCHEMA_NAME}: entry schema cannot be read")
        return None, errors
    except UnicodeError:
        errors.append(f"{ENTRY_SCHEMA_NAME}: entry schema is not valid UTF-8")
        return None, errors
    try:
        schema = parse_schema_json(encoded)
    except InvalidSchemaJSON:
        errors.append(f"{ENTRY_SCHEMA_NAME}: entry schema is not valid JSON")
        return None, errors
    if not isinstance(schema, dict):
        errors.append(f"{ENTRY_SCHEMA_NAME}: entry schema must be a JSON object")
        return None, errors
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except (jsonschema.SchemaError, RecursionError):
        errors.append(
            f"{ENTRY_SCHEMA_NAME}: entry schema is not valid Draft 2020-12 JSON Schema"
        )
        return None, errors
    try:
        validate_closed_schema_references(schema)
    except UnsafeSchemaReferences:
        errors.append(ENTRY_SCHEMA_EVALUATION_ERROR)
        return None, errors
    return (
        jsonschema.Draft202012Validator(
            schema,
            format_checker=jsonschema.FormatChecker(),
            registry=Registry(),
        ),
        errors,
    )


def entry_schema_violations(
    validator: jsonschema.Draft202012Validator,
    entry: Mapping[str, Any],
) -> list[jsonschema.ValidationError]:
    """Return ordered violations or raise a closed policy-state exception.

    The loader rejects every recognized open, recursive, or non-schema target
    before constructing a validator. This remains a defense-in-depth boundary:
    a future library exception or reference form must still become one closed
    policy error rather than a traceback.
    """
    try:
        return sorted(validator.iter_errors(entry), key=lambda error: list(error.path))
    except Exception as error:
        raise EntrySchemaUnevaluable from error


def _mapping(value: object) -> Mapping[str, Any]:
    """Return a JSON object as a mapping, or an empty mapping otherwise."""
    return value if isinstance(value, dict) else {}


def _canonical_repository(repository: str) -> str:
    normalized = repository.lower()
    return REPOSITORY_ALIASES.get(normalized, normalized)


def _preservation_sources(data: Mapping[str, Any]) -> list[tuple[str, str]]:
    source = _mapping(data.get("source"))
    candidates: list[tuple[object, object]] = [
        (source.get("repository"), source.get("commit"))
    ]
    formalization = _mapping(data.get("formalization"))
    dependencies = formalization.get("project_dependencies", [])
    if isinstance(dependencies, list):
        for value in dependencies:
            dependency = _mapping(value)
            if "path" not in dependency:
                candidates.append((dependency.get("repository"), dependency.get("revision")))
    substantive = _mapping(
        _mapping(data.get("provenance")).get("substantive_formalization")
    )
    if substantive:
        candidates.append((substantive.get("repository"), substantive.get("commit")))

    unique: dict[tuple[str, str], tuple[str, str]] = {}
    for repository, commit in candidates:
        if isinstance(repository, str) and isinstance(commit, str):
            unique.setdefault((repository.casefold(), commit), (repository, commit))
    return sorted(unique.values(), key=lambda item: (item[0].casefold(), item[1]))


def preservation_errors(name: str, data: Mapping[str, Any]) -> list[str]:
    """Check that preservation exactly covers the entry's external sources."""
    errors: list[str] = []
    preservation = _mapping(data.get("preservation"))
    repositories = preservation.get("repositories")
    if not isinstance(repositories, list):
        return errors
    expected_sources = _preservation_sources(data)
    actual_sources: list[tuple[object, object]] = []
    seen: set[tuple[str, str]] = set()
    identifier = data.get("id")
    version = data.get("version")
    for position, value in enumerate(repositories):
        row = _mapping(value)
        repository = row.get("source_repository")
        commit = row.get("commit")
        actual_sources.append((repository, commit))
        if isinstance(repository, str) and isinstance(commit, str):
            key = (repository.casefold(), commit)
            if key in seen:
                errors.append(
                    f"{name}:preservation.repositories.{position}: repository commit is duplicated"
                )
            seen.add(key)
            expected_ref = f"refs/tags/palomar/{identifier}-v{version}/{commit}"
            if row.get("ref") != expected_ref:
                errors.append(
                    f"{name}:preservation.repositories.{position}.ref: must be {expected_ref}"
                )
        fork = row.get("fork_repository")
        if isinstance(fork, str) and not fork.startswith("PalomarArchive/"):
            errors.append(
                f"{name}:preservation.repositories.{position}.fork_repository: "
                "must belong to PalomarArchive"
            )
    if actual_sources != expected_sources:
        errors.append(
            f"{name}:preservation.repositories: must exactly cover source, Git dependencies, "
            "and substantive formalization in canonical order"
        )
    return errors


def _validate_relative_path(
    errors: list[str], name: str, field: str, value: object, *, allow_dot: bool = False
) -> None:
    """Require a repository-relative POSIX path that cannot retarget a link."""
    if not isinstance(value, str):
        return
    if allow_dot and value == ".":
        return
    path = pathlib.PurePosixPath(value)
    if (
        not value
        or not path.parts
        or path.is_absolute()
        or value.startswith(("//", "\\"))
        or "\\" in value
        or ".." in path.parts
        or ":" in path.parts[0]
        or "?" in value
        or "#" in value
        or path.as_posix() != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        errors.append(f"{name}:{field}: must be a safe repository-relative path")


def entry_consistency_errors(name: str, data: Mapping[str, Any]) -> list[str]:
    """Check security-relevant relationships that JSON Schema cannot express."""
    errors = preservation_errors(name, data)
    source = _mapping(data.get("source"))
    verification = _mapping(data.get("verification"))
    formalization = _mapping(data.get("formalization"))
    trust = _mapping(data.get("trust"))
    provenance = _mapping(data.get("provenance"))

    source_repository = source.get("repository")
    source_commit = source.get("commit")
    source_repository_url = source.get("repository_url")
    if isinstance(source_repository, str) and GITHUB_REPOSITORY_RE.fullmatch(source_repository):
        expected_repository_url = f"https://github.com/{source_repository}"
        if source_repository_url != expected_repository_url:
            errors.append(
                f"{name}:source.repository_url: must be {expected_repository_url}"
            )
    if isinstance(source_commit, str) and not COMMIT_RE.fullmatch(source_commit):
        errors.append(f"{name}:source.commit: must be a full lowercase hexadecimal commit")
    project_path = source.get("project_path")
    if project_path is not None:
        _validate_relative_path(errors, name, "source.project_path", project_path)
    if isinstance(source_repository_url, str) and isinstance(source_commit, str):
        expected_tree_url = f"{source_repository_url}/tree/{source_commit}"
        if isinstance(project_path, str):
            encoded_project = "/".join(
                quote(segment, safe="") for segment in project_path.split("/")
            )
            expected_tree_url = f"{expected_tree_url}/{encoded_project}"
        if source.get("tree_url") != expected_tree_url:
            errors.append(f"{name}:source.tree_url: must be {expected_tree_url}")

    license_record = _mapping(source.get("license"))
    license_path = license_record.get("path")
    if not isinstance(license_path, str) or not LICENSE_PATH_RE.fullmatch(license_path):
        errors.append(
            f"{name}:source.license.path: must be a conventional root licence filename"
        )
    declared = license_record.get("declared_identifier")
    detected = license_record.get("detected_identifier")
    if isinstance(declared, str) and isinstance(detected, str) and declared != detected:
        errors.append(
            f"{name}:source.license: declared_identifier must equal detected_identifier"
        )

    substantive = _mapping(provenance.get("substantive_formalization"))
    substantive_repository = substantive.get("repository")
    substantive_commit = substantive.get("commit")
    if isinstance(substantive_repository, str) and GITHUB_REPOSITORY_RE.fullmatch(
        substantive_repository
    ):
        expected_repository_url = f"https://github.com/{substantive_repository}"
        if substantive.get("repository_url") != expected_repository_url:
            errors.append(
                f"{name}:provenance.substantive_formalization.repository_url: "
                f"must be {expected_repository_url}"
            )
        if isinstance(substantive_commit, str):
            expected_tree_url = f"{expected_repository_url}/tree/{substantive_commit}"
            if substantive.get("tree_url") != expected_tree_url:
                errors.append(
                    f"{name}:provenance.substantive_formalization.tree_url: "
                    f"must be {expected_tree_url}"
                )

    # The run URL is derived from the fixed verifier repository and run id, so
    # the three facts cannot disagree independently.
    repository = verification.get("repository")
    if repository != VERIFICATION_REPOSITORY:
        errors.append(
            f"{name}:verification.repository: must be {VERIFICATION_REPOSITORY}"
        )
    run_id = verification.get("run_id")
    if repository == VERIFICATION_REPOSITORY and isinstance(run_id, int):
        expected_run_url = f"https://github.com/{repository}/actions/runs/{run_id}"
        if verification.get("workflow_url") != expected_run_url:
            errors.append(f"{name}:verification.workflow_url: must be {expected_run_url}")

    for field in (
        "challenge_path",
        "solution_path",
        "comparator_config_path",
        "formalization_metadata_path",
    ):
        _validate_relative_path(
            errors, name, f"formalization.{field}", formalization.get(field)
        )
    _validate_relative_path(
        errors, name, "formalization.lakefile_path", formalization.get("lakefile_path")
    )
    if isinstance(project_path, str):
        project_prefix = f"{project_path}/"
        for field in (
            "challenge_path",
            "solution_path",
            "comparator_config_path",
            "lakefile_path",
        ):
            value = formalization.get(field)
            if isinstance(value, str) and not value.startswith(project_prefix):
                errors.append(
                    f"{name}:formalization.{field}: must be inside source.project_path"
                )
    lakefile_path = formalization.get("lakefile_path")
    expected_lakefiles = {
        f"{project_path}/{filename}" if isinstance(project_path, str) else filename
        for filename in ("lakefile.toml", "lakefile.lean")
    }
    if isinstance(lakefile_path, str) and lakefile_path not in expected_lakefiles:
        errors.append(
            f"{name}:formalization.lakefile_path: must name the selected project's Lakefile"
        )

    seen_dependency_names: set[str] = set()
    seen_dependency_paths: set[str] = set()
    dependencies = formalization.get("project_dependencies", [])
    if isinstance(dependencies, list):
        for position, value in enumerate(dependencies):
            dependency = _mapping(value)
            dependency_name = dependency.get("name")
            dependency_path = dependency.get("path")
            field = f"formalization.project_dependencies.{position}"
            if isinstance(dependency_name, str):
                if dependency_name in seen_dependency_names:
                    errors.append(f"{name}:{field}.name: dependency name is duplicated")
                seen_dependency_names.add(dependency_name)
            if isinstance(dependency_path, str):
                _validate_relative_path(
                    errors, name, f"{field}.path", dependency_path, allow_dot=True
                )
                if dependency_path in seen_dependency_paths:
                    errors.append(f"{name}:{field}.path: dependency path is duplicated")
                seen_dependency_paths.add(dependency_path)

    project_repositories = {
        _canonical_repository(repository)
        for dependency in formalization.get("project_dependencies", [])
        if isinstance(dependency, dict)
        and isinstance((repository := dependency.get("repository")), str)
    }
    seen_challenge_repositories: set[str] = set()
    has_qualified_dependency = False
    challenge_dependencies = trust.get("challenge_dependencies", [])
    if isinstance(challenge_dependencies, list):
        for position, value in enumerate(challenge_dependencies):
            dependency = _mapping(value)
            repository = dependency.get("repository")
            provenance = dependency.get("provenance")
            palomar_id = dependency.get("palomar_id")
            field = f"trust.challenge_dependencies.{position}"
            if not isinstance(repository, str):
                continue
            canonical = _canonical_repository(repository)
            if canonical in seen_challenge_repositories:
                errors.append(
                    f"{name}:{field}.repository: challenge repository is duplicated"
                )
            seen_challenge_repositories.add(canonical)
            if canonical not in project_repositories:
                errors.append(
                    f"{name}:{field}.repository: must occur in formalization.project_dependencies"
                )
            if provenance == "allowlisted":
                if palomar_id is not None:
                    errors.append(
                        f"{name}:{field}.palomar_id: forbidden for allowlisted provenance"
                    )
                if canonical not in {
                    repository.lower() for repository in HIGH_TRUST_CHALLENGE_REPOSITORIES
                }:
                    has_qualified_dependency = True
            elif provenance == "palomar-indexed":
                # Preserve a precise domain error when semantic checks run
                # alongside the sole schema's enum validation.
                errors.append(
                    f"{name}:{field}.provenance: palomar-indexed is no longer supported"
                )

    trust_level = trust.get("level")
    if trust_level == "high" and has_qualified_dependency:
        errors.append(
            f"{name}:trust.level: high trust permits only high-trust allowlisted dependencies"
        )
    return errors
