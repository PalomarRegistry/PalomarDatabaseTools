#!/usr/bin/env python3
"""Validate the Palomar database, completely or over one append-only delta.

An unscoped run checks every canonical entry, immutable bundle, and segmented
registration projection. ``--since`` checks only the new records and the exact
result, submission, and day projection transitions they require. Anything
outside that closed shape takes the complete path.

Historical immutability remains the separate responsibility of
`tools/check_append_only.py`; the base revision here is used only to prove the
closed validation delta. See `docs/append-only.md`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys
from collections.abc import Mapping
from typing import Any

import bundle_reference_validation
import changed_records
import correction_validation
import evidence_validation
import render_validation
import registration_projection
import score_validation
import validation_scope

from entry_validation import (
    EntrySchemaUnevaluable,
    ENTRY_SCHEMA_NAMES,
    ENTRY_SCHEMA_EVALUATION_ERROR,
    PALOMAR_ID_RE,
    entry_schema_violations,
    entry_consistency_errors,
    load_entry_schemas,
)
from takedowns import load_takedowns


# The same file `tools/check_append_only.py` reads, named here too because this
# is the other check a reader of a CI run looks at, and between them they are
# the whole of what CI says about the database.
LAUNCH_MARKER = ".palomar-launched"


def _registration_date_errors(
    entries: list[tuple[str, Mapping[str, Any]]],
    first: Mapping[str, str],
) -> list[str]:
    """Apply the shared date rule once v1 registration instants are known."""
    errors: list[str] = []
    for name, entry in entries:
        identifier = entry.get("id")
        first_registered_on = entry.get("first_registered_on")
        registered_at = entry.get("registered_at")
        if not isinstance(identifier, str) or not isinstance(first_registered_on, str):
            continue
        if isinstance(registered_at, str) and registered_at[:10] < first_registered_on:
            errors.append(
                f"{name}:registered_at: {registered_at} is before {first_registered_on}, "
                "the day the result entered the registry"
            )
        first_registered_at = first.get(identifier)
        # A result whose version 1 is absent is a database that is already
        # wrong in a way the version indexes report, and guessing the day from
        # a later version would hide it behind a second complaint.
        if first_registered_at is None:
            continue
        if first_registered_on != first_registered_at[:10]:
            errors.append(
                f"{name}:first_registered_on: {first_registered_on} is not the day {identifier}-v1 "
                f"was registered ({first_registered_at})"
            )
    return errors


def _validate_registration_dates(entries: list[tuple[str, Mapping[str, Any]]]) -> list[str]:
    """`first_registered_on` must be the day version 1 was registered.

    The two are one fact written twice. `first_registered_on` is the result's date: it
    is in the identifier, it decides the browse page, and every later version
    inherits it. `registered_at` is the version's own instant and is what every
    ordering surface reads. They meet at version 1, where the day of the one is
    the other.

    Nothing else would notice them coming apart. A record whose `first_registered_on`
    disagrees with its v1's `registered_at` is well formed, satisfies its
    schema, and passes the check that the identifier's date matches
    `first_registered_on`; what it produces is browsing that pages a result under one
    day while the landing page, the feeds and the subject pages order it under
    another, each of them correct on its own terms. Two fields that must agree
    and are written in different places, in two repositories, drift.

    A later version has its own instant and inherits the date, so for it the
    two only have to be in order. A version registered before its result
    entered the registry sorts behind the versions it supersedes, on every
    surface that carries it.
    """
    first: dict[str, str] = {}
    for _name, entry in entries:
        identifier, version = entry.get("id"), entry.get("version")
        registered_at = entry.get("registered_at")
        if version == 1 and isinstance(identifier, str) and isinstance(registered_at, str):
            first[identifier] = registered_at
    return _registration_date_errors(entries, first)


FULL_CHECKOUT_EXIT = 3


def validate(
    root: pathlib.Path,
    scope: validation_scope.ValidationScope | None = None,
) -> list[str]:
    """Return every problem found in the database rooted at `root`.

    With a `scope`, only new immutable records and their exact segmented
    registration transitions are checked. That is sound because prior main
    has already been fully validated, every record is immutable, and the sole
    entry schema is frozen; `validation_scope.scope_of` refuses to scope when
    those premises do not hold. The unscoped run remains the whole-registry
    scheduled proof.
    """
    errors: list[str] = []

    # A scoped publication may have only the fixed schema path materialized,
    # so the schema owner's glob and the retired-index existence check below
    # see that sparse view. This is still closed: adding any other schema or a
    # root index is an unexpected committed path, and
    # `validation_scope.scope_of` selects the complete checkout before
    # validation. The unscoped scheduled sweep checks the complete tree
    # independently.
    validators, schema_errors = load_entry_schemas(root)
    errors.extend(schema_errors)
    # Keep cross-field and bundle checks running after one hostile reference
    # disables schema evaluation; setting validator to None would skip them.
    schema_evaluable = bool(validators)
    expected_render_roots: set[str] = set()
    expected_evidence_roots: set[str] = set()
    loaded_entries: list[tuple[str, Mapping[str, Any]]] = []
    entry_targets: set[tuple[str, int]] = set()
    touched_render_roots = (
        frozenset()
        if scope is None
        else changed_records.changed_bundle_roots(scope.frozen_paths, "renders")
    )
    touched_evidence_roots = (
        frozenset()
        if scope is None
        else changed_records.changed_bundle_roots(scope.frozen_paths, "evidence")
    )

    # An empty registry is a legitimate state: nothing has been published yet.
    entries_dir = root / "entries"
    entry_paths = (
        sorted(entries_dir.iterdir() if entries_dir.is_dir() else [])
        if scope is None
        else [root / relative for relative in sorted(scope.entries)]
    )
    for path in entry_paths:
        name = f"entries/{path.name}"
        if path.is_symlink():
            # A symlinked entry would freeze its target string rather than the
            # bytes a consumer reads through it. See docs/append-only.md.
            errors.append(f"{name}: entries must be ordinary files, not symbolic links")
            continue
        if not path.is_file() or path.suffix != ".json":
            errors.append(
                f"{name}: entries/ holds PALOMAR-YYYY-MM-DD-NNNNNN-vN.json files only"
            )
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            errors.append(f"{name}: invalid JSON: {error}")
            continue
        if not isinstance(data, dict):
            errors.append(f"{name}: entry must be a JSON object")
            continue

        render = data.get("challenge_render")
        if isinstance(render, dict) and isinstance(render.get("artifact_path"), str):
            artifact_path = render["artifact_path"]
            if re.fullmatch(
                rf"renders/{PALOMAR_ID_RE.pattern}-v[1-9][0-9]*/[0-9a-f]{{64}}/",
                artifact_path,
            ):
                expected_render_roots.add(artifact_path.rstrip("/"))
        verification = data.get("verification")
        if isinstance(verification, dict) and isinstance(verification.get("evidence_path"), str):
            evidence_path = verification["evidence_path"]
            if re.fullmatch(
                rf"evidence/{PALOMAR_ID_RE.pattern}-v[1-9][0-9]*/[0-9a-f]{{64}}/",
                evidence_path,
            ):
                expected_evidence_roots.add(evidence_path.rstrip("/"))
        correction = data.get("registry_correction")
        if isinstance(correction, dict) and isinstance(correction.get("evidence_path"), str):
            correction_path = correction["evidence_path"]
            if re.fullmatch(
                rf"evidence/{PALOMAR_ID_RE.pattern}-v[1-9][0-9]*/[0-9a-f]{{64}}/",
                correction_path,
            ):
                expected_evidence_roots.add(correction_path.rstrip("/"))

        expected_name = f"{data.get('id')}-v{data.get('version')}.json"
        if path.name != expected_name:
            errors.append(f"{name}: filename must be {expected_name}")

        version = data.get("schema_version")
        if type(version) is not int or version not in ENTRY_SCHEMA_NAMES:
            errors.append(
                f"{name}: schema_version must be one of "
                f"{', '.join(map(str, ENTRY_SCHEMA_NAMES))}"
            )
        elif version in validators:
            if schema_evaluable:
                try:
                    violations = entry_schema_violations(validators[version], data)
                except EntrySchemaUnevaluable:
                    errors.append(
                        f"{ENTRY_SCHEMA_NAMES[version]}: {ENTRY_SCHEMA_EVALUATION_ERROR}"
                    )
                    schema_evaluable = False
                else:
                    for error in violations:
                        where = ".".join(str(part) for part in error.path) or "<root>"
                        errors.append(f"{name}:{where}: {error.message}")
            identifier = data.get("id")
            first_registered_on = data.get("first_registered_on")
            if (
                isinstance(identifier, str)
                and isinstance(first_registered_on, str)
                and not identifier.startswith(f"PALOMAR-{first_registered_on}-")
            ):
                errors.append(f"{name}:id: date must match first_registered_on")
            render_touched = scope is None or name in scope.entries
            evidence_touched = scope is None or name in scope.entries
            if scope is not None:
                if isinstance(render, dict) and isinstance(
                    render.get("artifact_path"), str
                ):
                    render_touched = (
                        render_touched
                        or render["artifact_path"].rstrip("/")
                        in touched_render_roots
                    )
                if isinstance(verification, dict) and isinstance(
                    verification.get("evidence_path"), str
                ):
                    evidence_touched = (
                        evidence_touched
                        or verification["evidence_path"].rstrip("/")
                        in touched_evidence_roots
                    )
            is_correction = isinstance(correction, dict)
            if render_touched and not is_correction:
                errors.extend(render_validation.validate_render(root, data, name))
            if evidence_touched and not is_correction:
                errors.extend(evidence_validation.validate_evidence(root, data, name))

        errors.extend(entry_consistency_errors(name, data))
        errors.extend(correction_validation.correction_errors(root, name, data))
        loaded_entries.append((name, data))
        identifier = data.get("id")
        entry_version = data.get("version")
        if (
            isinstance(identifier, str)
            and isinstance(entry_version, int)
            and not isinstance(entry_version, bool)
        ):
            entry_targets.add((identifier, entry_version))

    if scope is None:
        _takedowns, takedown_errors = load_takedowns(root, entry_targets)
    else:
        # `takedowns.json` is a validation_scope.GLOBAL_POLICY_PATH: any byte
        # change makes `validation_scope.scope_of` return None. A scope
        # therefore means the manifest is the exact one already checked at the
        # validated base, and re-opening its growing bytes would undo the
        # bounded accepted path. Publication adds the stronger served-parent
        # Git-blob binding in `validation_scope.scoped_parent_errors`; ordinary
        # PR validation relies on this explicit induction instead.
        takedown_errors = []
    errors.extend(takedown_errors)
    if scope is None:
        errors.extend(_validate_registration_dates(loaded_entries))
    else:
        first: dict[str, str] = {}
        identifiers = {
            entry.get("id")
            for _name, entry in loaded_entries
            if isinstance(entry.get("id"), str)
        }
        for identifier in identifiers:
            try:
                result = registration_projection.load_result(root, identifier)
            except ValueError:
                continue
            versions = result.get("versions")
            if isinstance(versions, list) and versions and isinstance(versions[0], dict):
                registered_at = versions[0].get("registered_at")
                if isinstance(registered_at, str):
                    first[identifier] = registered_at
        errors.extend(_registration_date_errors(loaded_entries, first))
    errors.extend(
        score_validation.validate_scores(
            root,
            loaded_entries,
            None if scope is None else scope.frozen_paths,
        )
    )

    errors.extend(
        bundle_reference_validation.validate_bundle_references(
            root,
            expected_render_roots,
            expected_evidence_roots,
            frozen_paths=None if scope is None else scope.frozen_paths,
        )
    )

    errors.extend(
        registration_projection.validate_projections(
            root,
            loaded_entries,
            base=None if scope is None else scope.base,
            changed_paths=frozenset() if scope is None else scope.registration_paths,
        )
    )
    if (root / "index.json").exists():
        errors.append("index.json: retired whole-registry authority must be absent")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=pathlib.Path,
        default=pathlib.Path("."),
        help="database root (default: the current directory)",
    )
    parser.add_argument(
        "--since",
        metavar="REV",
        help="validate the exact accepted delta since REV where that is sound; "
        "every uncertainty falls back to checking everything",
    )
    parser.add_argument(
        "--sparse-paths",
        type=pathlib.Path,
        help="classify --since without validating record bytes and write the exact "
        "current paths for a sparse accepted publication, NUL-delimited. Exit "
        f"{FULL_CHECKOUT_EXIT} requests the complete checkout instead",
    )
    parser.add_argument(
        "--sparse-dependencies",
        type=pathlib.Path,
        help="after --sparse-paths has been materialized, write the exact unchanged "
        "correction-baseline and registration-identity dependencies, NUL-delimited",
    )
    parser.add_argument(
        "--previous-base",
        type=pathlib.Path,
        help="bind a scoped publication to the authenticated served publication base; "
        "requires --since",
    )
    args = parser.parse_args(argv)
    if args.previous_base is not None and args.since is None:
        parser.error("--previous-base requires --since")

    if args.sparse_paths is not None or args.sparse_dependencies is not None:
        if args.since is None:
            parser.error("sparse path planning requires --since")
        if args.sparse_paths is not None and args.sparse_dependencies is not None:
            parser.error("choose --sparse-paths or --sparse-dependencies")
        output = args.sparse_paths or args.sparse_dependencies
        assert output is not None
        scope = validation_scope.scope_of(args.root, args.since)
        output.parent.mkdir(parents=True, exist_ok=True)
        if scope is None:
            output.write_bytes(b"")
            print("complete checkout required")
            return FULL_CHECKOUT_EXIT
        if args.previous_base is not None:
            parent_errors = validation_scope.scoped_parent_errors(
                args.root, scope, args.previous_base
            )
            if parent_errors:
                output.write_bytes(b"")
                print(f"complete checkout required: {parent_errors[0]}")
                return FULL_CHECKOUT_EXIT
        paths = (
            validation_scope.sparse_checkout_paths(scope)
            if args.sparse_paths is not None
            else validation_scope.sparse_dependency_paths(args.root, scope)
        )
        encoded = b"".join(path.encode("utf-8") + b"\0" for path in paths)
        output.write_bytes(encoded)
        kind = (
            "accepted checkout"
            if args.sparse_paths is not None
            else "dependency checkout"
        )
        print(f"sparse {kind}: {len(paths)} current path(s)")
        return 0

    # Said here as well as by the append-only check, because this is the job a
    # reader opens and the two answers are easy to confuse: this one says every
    # record is well formed, and that is all it has ever said. While the marker
    # is absent, nothing says any of them will still be there tomorrow, and a
    # run in which both jobs are green looks the same either way.
    if not (args.root / LAUNCH_MARKER).is_file():
        print(
            f"::warning::append-only enforcement is OFF: {LAUNCH_MARKER} is absent, so a "
            "published record may be rewritten or deleted and nothing here or in the "
            "append-only check will say so. This is deliberate before launch; committing "
            "the marker is a launch step, and docs/append-only.md has the order to do it in."
        )
    scope = validation_scope.scope_of(args.root, args.since) if args.since else None
    parent_errors = (
        validation_scope.scoped_parent_errors(args.root, scope, args.previous_base)
        if scope is not None and args.previous_base is not None
        else []
    )
    if scope is not None:
        print(
            f"checking all entry metadata and {len(scope.entries)} changed record bundle(s)"
        )
    errors = parent_errors + validate(args.root, scope)
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        return 1
    print("database is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
