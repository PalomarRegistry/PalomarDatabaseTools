#!/usr/bin/env python3
"""Fail fast when the source-availability runtime is incomplete."""

from __future__ import annotations

import importlib
import pathlib
import sys

# This module is installed as a top-level command module. Its own file then
# lives in site-packages, while the schemas it checks belong to the consumer
# database checkout. Commands run from that checkout, so the default root is
# the working directory rather than the package installation directory.
ROOT = pathlib.Path.cwd()

from entry_validation import ENTRY_SCHEMA_NAME, load_entry_schema
from score_validation import SCORES_SCHEMA_NAME, load_score_schema


def _schema_formats(value: object) -> set[str]:
    """Every string-valued JSON Schema ``format`` below ``value``."""
    found: set[str] = set()
    if isinstance(value, dict):
        format_name = value.get("format")
        if isinstance(format_name, str):
            found.add(format_name)
        for child in value.values():
            found.update(_schema_formats(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_schema_formats(child))
    return found


def main() -> int:
    # boto3 is deliberately lazy because only the credentialed operation needs
    # it. Import it explicitly with every availability module so CI exercises
    # the complete command's environment without touching R2 or the network.
    # That operation is publishing the target set, which is two commands: one
    # builds it from the canonical records and one puts it in the bucket. The
    # manifest itself is refreshed over HTTPS through the Worker, so it needs a
    # writer token but no R2 credential.
    for module in (
        "boto3",
        "check_source_availability",
        "public_source_availability",
        "publish_availability_targets",
        "source_availability_contract",
        "source_availability_targets",
    ):
        importlib.import_module(module)

    jsonschema = importlib.import_module("jsonschema")
    checker = jsonschema.FormatChecker()
    active_formats: set[str] = set()

    entry_validator, entry_errors = load_entry_schema(ROOT)
    if entry_errors:
        for error in entry_errors:
            print(error, file=sys.stderr)
        return 1
    if entry_validator is None:
        print(
            f"{ENTRY_SCHEMA_NAME}: entry schema loader returned no validator",
            file=sys.stderr,
        )
        return 1
    active_formats.update(_schema_formats(entry_validator.schema))

    # Schema discovery and failure behavior belong to their validation owners;
    # this runtime smoke consumes those policies instead of maintaining loaders
    # that can disagree with database validation.
    score_validator, score_errors = load_score_schema(ROOT, required=True)
    if score_errors:
        for error in score_errors:
            print(error, file=sys.stderr)
        return 1
    if score_validator is None:
        print(
            f"{SCORES_SCHEMA_NAME}: score schema loader returned no validator",
            file=sys.stderr,
        )
        return 1
    active_formats.update(_schema_formats(score_validator.schema))

    missing = sorted(active_formats - checker.checkers.keys())
    if missing:
        raise RuntimeError("jsonschema has no checker for: " + ", ".join(missing))

    # Membership above proves that every active format is registered. These
    # behavioral probes additionally prove that the URI and date checkers do
    # not merely accept malformed values.
    malformed_formats = (("date", "2026-99-99"), ("uri", "not a URI"))
    for format_name, malformed in malformed_formats:
        try:
            checker.check(malformed, format_name)
        except jsonschema.exceptions.FormatError:
            continue
        raise RuntimeError(f"jsonschema has no working {format_name!r} format checker")

    print(
        "source-availability runtime imports and schema format checkers are ready: "
        + ", ".join(sorted(active_formats))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
