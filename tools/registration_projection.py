"""Closed, segmented authority for registered result identity and allocation.

The registry deliberately has no whole-database registration index. One result
document records ordered versions, one immutable identity document owns the
stable repository/project/configuration tuple, one immutable submission
document binds an intake id to the version it created, and one small day
document holds the last allocated serial. An accepted event therefore touches
only documents local to that identity, result, submission, and day.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import pathlib
import re
import stat
import subprocess
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

SCHEMA_VERSION = 2
RESULTS_DIRECTORY = "registrations/results"
SUBMISSIONS_DIRECTORY = "registrations/submissions"
DAYS_DIRECTORY = "registrations/days"
IDENTITIES_DIRECTORY = "registrations/identities"
PALOMAR_ID_PATTERN = r"PALOMAR-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}"
RESULT_PATH_RE = re.compile(rf"{RESULTS_DIRECTORY}/({PALOMAR_ID_PATTERN})\.json")
SUBMISSION_PATH_RE = re.compile(rf"{SUBMISSIONS_DIRECTORY}/([a-z0-9]{{12}})\.json")
DAY_PATH_RE = re.compile(rf"{DAYS_DIRECTORY}/([0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}})\.json")
IDENTITY_PATH_RE = re.compile(rf"{IDENTITIES_DIRECTORY}/([0-9a-f]{{64}})\.json")
ID_PARTS_RE = re.compile(r"PALOMAR-([0-9]{4}-[0-9]{2}-[0-9]{2})-([0-9]{6})")
MAX_VERSIONS_PER_RESULT = 500


class _InvalidProjectionJSON(ValueError):
    pass


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise _InvalidProjectionJSON
        document[key] = value
    return document


def _reject_nonfinite(_value: str) -> None:
    raise _InvalidProjectionJSON


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise _InvalidProjectionJSON
    return parsed


def _strict_json(encoded: str | bytes, label: str) -> dict[str, Any]:
    """Decode one authority document without ambiguous JSON extensions."""
    try:
        value = json.loads(
            encoded,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_nonfinite,
            parse_float=_finite_float,
        )
    except (RecursionError, UnicodeDecodeError, ValueError) as error:
        raise ValueError(f"{label}: is not valid strict JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label}: must be a JSON object")
    return value


def result_path(identifier: str) -> str:
    return f"{RESULTS_DIRECTORY}/{identifier}.json"


def submission_path(submission_id: str) -> str:
    return f"{SUBMISSIONS_DIRECTORY}/{submission_id}.json"


def day_path(day: str) -> str:
    return f"{DAYS_DIRECTORY}/{day}.json"


def identity_digest(identity: Mapping[str, Any]) -> str:
    preimage = "\0".join(
        (
            str(identity["source_repository"]),
            str(identity["project_path"] or ""),
            str(identity["comparator_config_path"]),
        )
    ).encode("utf-8")
    return hashlib.sha256(preimage).hexdigest()


def identity_path(identity: Mapping[str, Any]) -> str:
    return f"{IDENTITIES_DIRECTORY}/{identity_digest(identity)}.json"


def _calendar_day(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return dt.date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _identity(entry: Mapping[str, Any]) -> dict[str, Any]:
    source = entry.get("source")
    formalization = entry.get("formalization")
    if not isinstance(source, Mapping) or not isinstance(formalization, Mapping):
        return {
            "source_repository": None,
            "project_path": None,
            "comparator_config_path": None,
        }
    repository = source.get("repository")
    return {
        # GitHub owner/repository identity is case-insensitive. Store its
        # canonical comparison key so a cosmetic case change cannot split one
        # result identity while a real rename still requires a new result.
        "source_repository": (
            repository.casefold() if isinstance(repository, str) else repository
        ),
        "project_path": source.get("project_path"),
        "comparator_config_path": formalization.get("comparator_config_path"),
    }


def version_summary(relative: str, entry: Mapping[str, Any]) -> dict[str, Any]:
    submission = entry.get("submission")
    return {
        "version": entry.get("version"),
        "submission_id": submission.get("submission_id")
        if isinstance(submission, Mapping)
        else None,
        "registered_at": entry.get("registered_at"),
        "title": entry.get("title"),
        "status": entry.get("status"),
        "path": relative,
        "abstract": entry.get("abstract"),
        "classification": entry.get("classification"),
    }


def public_summary(row: Mapping[str, Any], identifier: str) -> dict[str, Any]:
    """The five-field summary used by public version and browse surfaces."""
    return {
        "id": identifier,
        "version": row.get("version"),
        "title": row.get("title"),
        "status": row.get("status"),
        "path": row.get("path"),
    }


def entry_view(row: Mapping[str, Any], identifier: str, first_registered_on: str) -> dict[str, Any]:
    """Projection fields needed to patch historical presentation surfaces."""
    return {
        "id": identifier,
        "version": row.get("version"),
        "first_registered_on": first_registered_on,
        "registered_at": row.get("registered_at"),
        "title": row.get("title"),
        "status": row.get("status"),
        "abstract": row.get("abstract"),
        "classification": row.get("classification"),
    }


def documents_for_entries(
    entries: Iterable[tuple[str, Mapping[str, Any]]],
    *,
    enforce_contract: bool = False,
) -> dict[str, dict[str, Any]]:
    """Derive the complete segmented authority from canonical entries."""
    by_result: dict[str, list[tuple[str, Mapping[str, Any]]]] = defaultdict(list)
    documents: dict[str, dict[str, Any]] = {}
    day_serials: dict[str, list[int]] = defaultdict(list)
    for relative, entry in entries:
        identifier = entry.get("id")
        version = entry.get("version")
        submission = entry.get("submission")
        submission_id = submission.get("submission_id") if isinstance(submission, Mapping) else None
        if (
            not isinstance(identifier, str)
            or ID_PARTS_RE.fullmatch(identifier) is None
            or type(version) is not int
        ):
            continue
        by_result[identifier].append((relative, entry))
        if isinstance(submission_id, str):
            binding = {
                "schema_version": SCHEMA_VERSION,
                "submission_id": submission_id,
                "id": identifier,
                "version": version,
                "entry_path": relative,
            }
            binding_path = submission_path(submission_id)
            if (
                enforce_contract
                and binding_path in documents
                and documents[binding_path] != binding
            ):
                raise ValueError(f"{binding_path}: submission_id is bound to two versions")
            documents[binding_path] = binding
        match = ID_PARTS_RE.fullmatch(identifier)
        if version == 1 and match is not None:
            day, serial = match.group(1), int(match.group(2))
            day_serials[day].append(serial)
    for identifier, rows in by_result.items():
        rows.sort(key=lambda item: int(item[1].get("version", 0)))
        first = rows[0][1]
        first_registered_on = first.get("first_registered_on")
        identity = _identity(first)
        if enforce_contract and [entry.get("version") for _relative, entry in rows] != list(
            range(1, len(rows) + 1)
        ):
            raise ValueError(f"{result_path(identifier)}: versions are not contiguous from 1")
        if enforce_contract and any(
            entry.get("first_registered_on") != first_registered_on or _identity(entry) != identity
            for _relative, entry in rows
        ):
            raise ValueError(f"{result_path(identifier)}: stable registration identity changed")
        result = {
            "schema_version": SCHEMA_VERSION,
            "id": identifier,
            "first_registered_on": first_registered_on,
            "identity": identity,
            "versions": [version_summary(relative, entry) for relative, entry in rows],
        }
        if enforce_contract:
            _validate_result_shape(result, identifier, result_path(identifier))
        documents[result_path(identifier)] = result
        authority = {
            "schema_version": SCHEMA_VERSION,
            "identity": identity,
            "registration_id": identifier,
        }
        authority_path = identity_path(identity)
        if enforce_contract and authority_path in documents:
            raise ValueError(
                f"{authority_path}: registration identity belongs to two results"
            )
        documents[authority_path] = authority
    for day, serials in day_serials.items():
        ordered = sorted(serials)
        if enforce_contract and ordered != list(range(1, len(ordered) + 1)):
            raise ValueError(
                f"{day_path(day)}: result serials are not contiguous from 1"
            )
        documents[day_path(day)] = {
            "schema_version": SCHEMA_VERSION,
            "date": day,
            "last_serial": max(serials),
        }
    return documents


def _load(path: pathlib.Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label}: must be an ordinary JSON file")
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        encoded = path.read_bytes()
    except OSError as error:
        raise ValueError(f"{label}: cannot be read") from error
    if mode & 0o111:
        raise ValueError(f"{label}: must be a non-executable ordinary file")
    return _strict_json(encoded, label)


def load_result(root: pathlib.Path, identifier: str) -> dict[str, Any]:
    relative = result_path(identifier)
    document = _load(root / relative, relative)
    _validate_result_shape(document, identifier, relative)
    return document


def _validate_result_shape(document: Mapping[str, Any], identifier: str, where: str) -> None:
    if set(document) != {"schema_version", "id", "first_registered_on", "identity", "versions"}:
        raise ValueError(f"{where}: has an unsupported result projection shape")
    if type(document.get("schema_version")) is not int or document["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"{where}: unsupported schema_version")
    if document.get("id") != identifier:
        raise ValueError(f"{where}: id disagrees with its path")
    first_registered_on = document.get("first_registered_on")
    identifier_parts = ID_PARTS_RE.fullmatch(identifier)
    if (
        not _calendar_day(first_registered_on)
        or identifier_parts is None
        or identifier_parts.group(1) != first_registered_on
    ):
        raise ValueError(f"{where}: first_registered_on disagrees with id")
    identity = document.get("identity")
    if not isinstance(identity, dict) or set(identity) != {
        "source_repository", "project_path", "comparator_config_path"
    }:
        raise ValueError(f"{where}: has a malformed stable identity")
    if not isinstance(identity.get("source_repository"), str) or not isinstance(
        identity.get("comparator_config_path"), str
    ) or (identity.get("project_path") is not None and not isinstance(identity.get("project_path"), str)):
        raise ValueError(f"{where}: has a malformed stable identity")
    versions = document.get("versions")
    if not isinstance(versions, list) or not versions:
        raise ValueError(f"{where}: versions must be a non-empty array")
    if len(versions) > MAX_VERSIONS_PER_RESULT:
        raise ValueError(
            f"{where}: has more than {MAX_VERSIONS_PER_RESULT} versions, "
            "which one result projection may not carry"
        )
    keys = {"version", "submission_id", "registered_at", "title", "status", "path", "abstract", "classification"}
    submissions: set[str] = set()
    for expected_version, row in enumerate(versions, 1):
        if not isinstance(row, dict) or set(row) != keys:
            raise ValueError(f"{where}: version {expected_version} has an unsupported shape")
        submission_id = row.get("submission_id")
        if type(row.get("version")) is not int or row["version"] != expected_version:
            raise ValueError(f"{where}: versions must be ordered from 1 without gaps")
        if not isinstance(submission_id, str) or not re.fullmatch(r"[a-z0-9]{12}", submission_id) or submission_id in submissions:
            raise ValueError(f"{where}: version {expected_version} has a malformed or repeated submission_id")
        submissions.add(submission_id)
        if row.get("path") != f"entries/{identifier}-v{expected_version}.json":
            raise ValueError(f"{where}: version {expected_version} path disagrees with identity")
        if not isinstance(row.get("registered_at"), str):
            raise ValueError(f"{where}: version {expected_version} has no registration instant")
        if not all(isinstance(row.get(field), str) for field in ("title", "status", "abstract")):
            raise ValueError(
                f"{where}: version {expected_version} has malformed presentation text"
            )
        classification = row.get("classification")
        if (
            not isinstance(classification, dict)
            or set(classification) != {"arxiv", "msc2020"}
            or any(
                not isinstance(values, list)
                or any(not isinstance(value, str) for value in values)
                for values in classification.values()
            )
        ):
            raise ValueError(
                f"{where}: version {expected_version} has malformed classification"
            )


def _validate_submission_shape(
    document: Mapping[str, Any], submission_id: str, where: str
) -> None:
    if set(document) != {"schema_version", "submission_id", "id", "version", "entry_path"}:
        raise ValueError(f"{where}: has an unsupported submission projection shape")
    if type(document.get("schema_version")) is not int or document["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"{where}: unsupported schema_version")
    if document.get("submission_id") != submission_id:
        raise ValueError(f"{where}: submission_id disagrees with its path")
    identifier, version = document.get("id"), document.get("version")
    if not isinstance(identifier, str) or ID_PARTS_RE.fullmatch(identifier) is None:
        raise ValueError(f"{where}: has a malformed result id")
    if type(version) is not int or version < 1:
        raise ValueError(f"{where}: has a malformed version")
    if document.get("entry_path") != f"entries/{identifier}-v{version}.json":
        raise ValueError(f"{where}: entry_path disagrees with its binding")


def _validate_day_shape(document: Mapping[str, Any], day: str, where: str) -> None:
    if set(document) != {"schema_version", "date", "last_serial"}:
        raise ValueError(f"{where}: has an unsupported day projection shape")
    if type(document.get("schema_version")) is not int or document["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"{where}: unsupported schema_version")
    if document.get("date") != day or not _calendar_day(day):
        raise ValueError(f"{where}: date disagrees with its path")
    last_serial = document.get("last_serial")
    if type(last_serial) is not int or not 1 <= last_serial <= 999999:
        raise ValueError(f"{where}: last_serial is outside the identifier range")


def _validate_identity_shape(
    document: Mapping[str, Any], digest: str, where: str
) -> None:
    if set(document) != {"schema_version", "identity", "registration_id"}:
        raise ValueError(f"{where}: has an unsupported identity projection shape")
    identity = document.get("identity")
    identifier = document.get("registration_id")
    if (
        type(document.get("schema_version")) is not int
        or document["schema_version"] != SCHEMA_VERSION
        or not isinstance(identity, dict)
        or set(identity)
        != {"source_repository", "project_path", "comparator_config_path"}
        or not isinstance(identity.get("source_repository"), str)
        or identity["source_repository"] != identity["source_repository"].casefold()
        or (identity.get("project_path") is not None and not isinstance(identity.get("project_path"), str))
        or not isinstance(identity.get("comparator_config_path"), str)
        or identity_digest(identity) != digest
        or not isinstance(identifier, str)
        or ID_PARTS_RE.fullmatch(identifier) is None
    ):
        raise ValueError(f"{where}: has a malformed identity binding")


def all_result_documents(root: pathlib.Path) -> list[dict[str, Any]]:
    """Load all result projections for an explicitly whole-registry operation."""
    directory = root / RESULTS_DIRECTORY
    if not directory.exists():
        return []
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError(f"{RESULTS_DIRECTORY}/: must be an ordinary directory")
    documents = []
    for path in sorted(directory.iterdir()):
        relative = path.relative_to(root).as_posix()
        match = RESULT_PATH_RE.fullmatch(relative)
        if match is None:
            raise ValueError(f"{relative}: unexpected result projection path")
        document = _load(path, relative)
        _validate_result_shape(document, match.group(1), relative)
        documents.append(document)
    return documents


def all_entry_summaries(root: pathlib.Path) -> list[dict[str, Any]]:
    return [
        public_summary(row, str(document["id"]))
        for document in all_result_documents(root)
        for row in document["versions"]
    ]


def _git_blob(root: pathlib.Path, revision: str, relative: str) -> dict[str, Any] | None:
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-tree", "-z", revision, "--", relative],
        check=False,
        capture_output=True,
    )
    if listed.returncode != 0:
        raise ValueError(f"{relative} at validated base cannot be resolved")
    if not listed.stdout:
        return None
    metadata, separator, found_path = listed.stdout.partition(b"\t")
    fields = metadata.split()
    if separator != b"\t" or found_path.rstrip(b"\0") != relative.encode() or len(fields) != 3:
        raise ValueError(f"{relative} at validated base cannot be resolved exactly")
    mode, object_type, object_id = fields
    if object_type != b"blob" or mode != b"100644":
        raise ValueError(
            f"{relative} at validated base must be an ordinary mode 100644 file"
        )
    blob = subprocess.run(
        ["git", "-C", str(root), "cat-file", "blob", object_id.decode("ascii")],
        check=False,
        capture_output=True,
    )
    if blob.returncode != 0:
        raise ValueError(f"{relative} at validated base cannot be read")
    return _strict_json(blob.stdout, f"{relative} at validated base")


def validate_projections(
    root: pathlib.Path,
    entries: list[tuple[str, Mapping[str, Any]]],
    *,
    base: str | None = None,
    changed_paths: frozenset[str] = frozenset(),
) -> list[str]:
    """Validate either the complete authority or one exact append transition."""
    try:
        if base is None:
            expected = documents_for_entries(entries, enforce_contract=True)
            actual_paths: set[str] = set()
            registration_root = root / "registrations"
            if registration_root.exists():
                if registration_root.is_symlink() or not registration_root.is_dir():
                    raise ValueError("registrations/: must be an ordinary directory")
                expected_children = {"results", "submissions", "days", "identities"}
                for child in registration_root.iterdir():
                    if child.name not in expected_children:
                        raise ValueError(
                            f"registrations/{child.name}: unexpected authority path"
                        )
            for directory, pattern in (
                (RESULTS_DIRECTORY, RESULT_PATH_RE),
                (SUBMISSIONS_DIRECTORY, SUBMISSION_PATH_RE),
                (DAYS_DIRECTORY, DAY_PATH_RE),
                (IDENTITIES_DIRECTORY, IDENTITY_PATH_RE),
            ):
                parent = root / directory
                if not parent.exists():
                    continue
                if parent.is_symlink() or not parent.is_dir():
                    raise ValueError(f"{directory}/: must be an ordinary directory")
                for path in parent.iterdir():
                    relative = path.relative_to(root).as_posix()
                    if pattern.fullmatch(relative) is None:
                        raise ValueError(f"{relative}: unexpected registration projection path")
                    actual_paths.add(relative)
            if actual_paths != set(expected):
                missing = sorted(set(expected) - actual_paths)
                extra = sorted(actual_paths - set(expected))
                raise ValueError(f"registrations/: projection paths disagree with entries; missing={missing}, extra={extra}")
            for relative, wanted in sorted(expected.items()):
                actual = _load(root / relative, relative)
                if actual != wanted:
                    raise ValueError(f"{relative}: does not exactly match canonical entries")
            return []

        touched_expected: dict[str, dict[str, Any]] = {}
        by_result: dict[str, list[tuple[str, Mapping[str, Any]]]] = defaultdict(list)
        for relative, entry in entries:
            identifier, version = entry.get("id"), entry.get("version")
            if isinstance(identifier, str) and type(version) is int:
                by_result[identifier].append((relative, entry))
        day_new: dict[str, list[int]] = defaultdict(list)
        for identifier, new_rows in by_result.items():
            if ID_PARTS_RE.fullmatch(identifier) is None:
                raise ValueError(f"entries name a malformed result id: {identifier!r}")
            new_rows.sort(key=lambda pair: int(pair[1].get("version", 0)))
            relative = result_path(identifier)
            if relative not in changed_paths:
                raise ValueError(
                    f"{relative}: appending a version must change its result projection"
                )
            prior = _git_blob(root, base, relative)
            prior_versions: list[Any] = []
            if prior is not None:
                _validate_result_shape(prior, identifier, relative)
                prior_versions = list(prior["versions"])
            first = new_rows[0][1] if prior is None else None
            first_registered_on = first.get("first_registered_on") if first is not None else prior["first_registered_on"]
            identity = _identity(first) if first is not None else prior["identity"]
            authority_relative = identity_path(identity)
            authority = {
                "schema_version": SCHEMA_VERSION,
                "identity": identity,
                "registration_id": identifier,
            }
            prior_authority = _git_blob(root, base, authority_relative)
            if prior is None:
                if authority_relative not in changed_paths:
                    raise ValueError(
                        f"{authority_relative}: allocating a result must bind its identity"
                    )
                if prior_authority is not None or authority_relative in touched_expected:
                    raise ValueError(
                        f"{authority_relative}: registration identity is already bound"
                    )
                touched_expected[authority_relative] = authority
            else:
                if prior_authority is None:
                    raise ValueError(
                        f"{authority_relative}: existing result has no identity binding"
                    )
                digest = IDENTITY_PATH_RE.fullmatch(authority_relative)
                assert digest is not None
                _validate_identity_shape(
                    prior_authority,
                    digest.group(1),
                    f"{authority_relative} at validated base",
                )
                if prior_authority != authority:
                    raise ValueError(
                        f"{authority_relative}: identity belongs to another result"
                    )
            appended = list(prior_versions)
            for entry_relative, entry in new_rows:
                version = entry.get("version")
                if type(version) is not int or version != len(appended) + 1:
                    raise ValueError(f"{relative}: changed entries are not the next ordered versions")
                if entry.get("first_registered_on") != first_registered_on or _identity(entry) != identity:
                    raise ValueError(f"{relative}: stable registration identity changed")
                row = version_summary(entry_relative, entry)
                appended.append(row)
                submission_id = row["submission_id"]
                if isinstance(submission_id, str):
                    binding_path = submission_path(submission_id)
                    if binding_path in touched_expected:
                        raise ValueError(
                            f"{binding_path}: submission_id is bound to two versions"
                        )
                    if binding_path not in changed_paths:
                        raise ValueError(
                            f"{binding_path}: new submission_id has no new binding "
                            "or is already bound"
                        )
                    prior_binding = _git_blob(root, base, binding_path)
                    if prior_binding is not None:
                        _validate_submission_shape(
                            prior_binding, submission_id, f"{binding_path} at validated base"
                        )
                        raise ValueError(
                            f"{binding_path}: submission_id is already bound at validated base"
                        )
                    touched_expected[binding_path] = {
                        "schema_version": SCHEMA_VERSION,
                        "submission_id": submission_id,
                        "id": identifier,
                        "version": version,
                        "entry_path": entry_relative,
                    }
                match = ID_PARTS_RE.fullmatch(identifier)
                if version == 1 and match is not None:
                    day_new[match.group(1)].append(int(match.group(2)))
            completed_result = {
                "schema_version": SCHEMA_VERSION,
                "id": identifier,
                "first_registered_on": first_registered_on,
                "identity": identity,
                "versions": appended,
            }
            _validate_result_shape(completed_result, identifier, relative)
            touched_expected[relative] = completed_result
        for day, serials in day_new.items():
            relative = day_path(day)
            if relative not in changed_paths:
                raise ValueError(
                    f"{relative}: allocating a result must change its day projection"
                )
            prior = _git_blob(root, base, relative)
            if prior is None:
                last = 0
            else:
                _validate_day_shape(prior, day, f"{relative} at validated base")
                last = prior["last_serial"]
            ordered = sorted(serials)
            if type(last) is not int or ordered != list(
                range(last + 1, last + len(ordered) + 1)
            ):
                raise ValueError(
                    f"{relative}: new result serials are not the next contiguous allocation"
                )
            touched_expected[relative] = {
                "schema_version": SCHEMA_VERSION,
                "date": day,
                "last_serial": last + len(ordered),
            }
        if set(changed_paths) != set(touched_expected):
            raise ValueError(
                "registrations/: changed projection paths do not exactly match new entries; "
                f"expected={sorted(touched_expected)}, actual={sorted(changed_paths)}"
            )
        for relative, wanted in sorted(touched_expected.items()):
            actual = _load(root / relative, relative)
            result_match = RESULT_PATH_RE.fullmatch(relative)
            submission_match = SUBMISSION_PATH_RE.fullmatch(relative)
            day_match = DAY_PATH_RE.fullmatch(relative)
            identity_match = IDENTITY_PATH_RE.fullmatch(relative)
            if result_match is not None:
                _validate_result_shape(actual, result_match.group(1), relative)
            elif submission_match is not None:
                _validate_submission_shape(actual, submission_match.group(1), relative)
            elif day_match is not None:
                _validate_day_shape(actual, day_match.group(1), relative)
            elif identity_match is not None:
                _validate_identity_shape(actual, identity_match.group(1), relative)
            else:  # guarded by exact touched paths; keep the owner fail closed
                raise ValueError(f"{relative}: unexpected registration projection path")
            if actual != wanted:
                raise ValueError(f"{relative}: is not the exact append transition")
        return []
    except ValueError as error:
        return [str(error)]
