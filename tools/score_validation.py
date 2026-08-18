"""Validate private score documents against their registered entries.

This module owns the score schema location, canonical score path, score
directory shape, per-review binding, and the exact one-score-per-entry
contract. It reads only the supplied database root, entries, and optional
append-only additions and returns errors without mutating them. Database
traversal and diff classification remain with validate.py.
"""

from __future__ import annotations

import json
import pathlib
import re
from collections.abc import Mapping
from typing import Any

import jsonschema
from referencing import Registry

from entry_validation import PALOMAR_ID_RE
from schema_policy import (
    InvalidSchemaJSON,
    UnsafeSchemaReferences,
    parse_schema_json,
    validate_closed_schema_references,
)

SCORES_SCHEMA_NAME = "scores-v1.json"
SCORE_SCHEMA_EVALUATION_ERROR = (
    f"{SCORES_SCHEMA_NAME}: score schema cannot be evaluated safely"
)
SCORE_PATH_RE = re.compile(
    rf"scores/{PALOMAR_ID_RE.pattern}-v[1-9][0-9]*\.json"
)


def _mapping(value: object) -> Mapping[str, Any]:
    """Return a JSON object as a mapping, or an empty mapping otherwise."""
    return value if isinstance(value, dict) else {}


def load_score_schema(
    root: pathlib.Path, *, required: bool
) -> tuple[jsonschema.Draft202012Validator | None, list[str]]:
    """Load the score schema without permitting an invalid policy fallback.

    The shared loader resolves the complete closed reference graph before any
    score exists, and an empty runtime registry is defense in depth. A schema
    is repository policy, so validation must not depend on mutable network
    content or turn an unreachable reference into an exception.
    """
    path = root / SCORES_SCHEMA_NAME
    if path.is_symlink() or not path.is_file():
        errors = (
            [f"{SCORES_SCHEMA_NAME}: file is missing, so no scores can be checked"]
            if required
            else []
        )
        return None, errors
    try:
        encoded = path.read_text(encoding="utf-8")
    except OSError:
        return None, [f"{SCORES_SCHEMA_NAME}: score schema cannot be read"]
    except UnicodeError:
        return None, [f"{SCORES_SCHEMA_NAME}: score schema is not valid UTF-8"]
    try:
        schema = parse_schema_json(encoded)
    except InvalidSchemaJSON:
        return None, [f"{SCORES_SCHEMA_NAME}: score schema is not valid JSON"]
    if not isinstance(schema, dict):
        return None, [f"{SCORES_SCHEMA_NAME}: score schema must be a JSON object"]
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except (jsonschema.SchemaError, RecursionError):
        return None, [
            f"{SCORES_SCHEMA_NAME}: score schema is not valid Draft 2020-12 JSON Schema"
        ]
    try:
        validate_closed_schema_references(schema)
    except UnsafeSchemaReferences:
        return None, [SCORE_SCHEMA_EVALUATION_ERROR]
    return (
        jsonschema.Draft202012Validator(
            schema,
            format_checker=jsonschema.FormatChecker(),
            registry=Registry(),
        ),
        [],
    )


def validate_scores(
    root: pathlib.Path,
    entries: list[tuple[str, Mapping[str, Any]]],
    frozen_paths: frozenset[str] | None = None,
) -> list[str]:
    """Check `scores/`, which is recorded but never served.

    The scores decide whether a version is registered, so they have to survive
    for the review outcome to be reconstructable. They are not part of the record,
    because a record's bytes have to be a function of the commit -- while the
    publisher projected them away, the same commit could serve different bytes
    after a publisher change, which is exactly what happened twice.

    Every registered version has exactly one scores file, and it is bound to
    the review it explains by `reviewed_at` and `policy_commit`. Without that
    binding a later pass could leave the numbers from an earlier one in place
    and nothing would say so.

    ``frozen_paths`` is the closed set of ordinary append-only additions. Its
    ``scores/`` members have already been restricted by the caller to canonical
    ``SCORE_PATH_RE`` paths. When present, every expected score is still checked
    for existence, but only score bytes in that set are reopened.
    """
    validator, errors = load_score_schema(root, required=bool(entries))
    scores_dir = root / "scores"

    expected: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for name, entry in entries:
        identifier, version = entry.get("id"), entry.get("version")
        if isinstance(identifier, str) and isinstance(version, int) and not isinstance(version, bool):
            expected[f"{identifier}-v{version}.json"] = (name, entry)

    if scores_dir.is_symlink():
        return errors + ["scores/: must be an ordinary directory, not a symbolic link"]
    if scores_dir.exists() and not scores_dir.is_dir():
        return errors + ["scores/: must be a directory"]

    present: set[str] = set()
    if frozen_paths is None:
        score_paths = sorted(scores_dir.iterdir() if scores_dir.is_dir() else [])
    else:
        # Scoped validation owns only the new entries and scores. Prior main is
        # the validated induction premise; the scheduled/full sweep reopens
        # every historical score and catches at-rest drift.
        present = {
            filename
            for filename in expected
            if (candidate := scores_dir / filename).is_file()
            and not candidate.is_symlink()
        }
        score_paths = [
            root / relative
            for relative in sorted(frozen_paths)
            if relative.startswith("scores/")
        ]
    for path in score_paths:
        name = f"scores/{path.name}"
        if path.is_symlink():
            errors.append(f"{name}: scores must be ordinary files, not symbolic links")
            continue
        if not path.is_file() or path.suffix != ".json":
            errors.append(f"{name}: scores/ holds PALOMAR-YYYY-MM-DD-NNNNNN-vN.json files only")
            continue
        if path.name not in expected:
            errors.append(f"{name}: scores are not recorded for any registered version")
            continue
        present.add(path.name)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            errors.append(f"{name}: invalid JSON: {error}")
            continue
        if not isinstance(data, dict):
            errors.append(f"{name}: scores must be a JSON object")
            continue
        if validator is not None:
            try:
                schema_errors = sorted(
                    validator.iter_errors(data), key=lambda error: list(error.path)
                )
            except Exception:
                # The loader closes the known reference graph first. Keep a
                # defense-in-depth boundary around future resolver or library
                # failures: policy remains untrusted input and every such
                # failure is one closed error.
                errors.append(SCORE_SCHEMA_EVALUATION_ERROR)
                validator = None
            else:
                for error in schema_errors:
                    where = ".".join(str(part) for part in error.path) or "<root>"
                    errors.append(f"{name}:{where}: {error.message}")
        entry_name, entry = expected[path.name]
        if (data.get("id"), data.get("version")) != (entry.get("id"), entry.get("version")):
            errors.append(f"{name}: identity disagrees with its filename")
        review = _mapping(entry.get("review"))
        for field in ("reviewed_at", "policy_commit"):
            if data.get(field) != review.get(field):
                errors.append(
                    f"{name}:{field}: must match {entry_name}, so that the scores "
                    "belong to the review they explain"
                )

    required = set(expected)
    for missing in sorted(required - present):
        errors.append(
            f"scores/{missing}: missing, but entries/{missing} is registered. "
            "Every registered version records the scores that decided it."
        )
    return errors
