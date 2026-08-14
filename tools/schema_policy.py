"""Strict JSON and closed-reference rules shared by repository schemas.

Schema documents are executable repository policy. They may use the current
contracts' acyclic local JSON-Pointer references, but not a second resource,
anchor, dynamic scope, or mutable network document. This boundary checks that
whole graph when policy is loaded, before the presence or shape of any record
can decide whether a bad branch is traversed.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote_to_bytes

import jsonschema
from referencing.jsonschema import DRAFT202012

DRAFT202012_URI = "https://json-schema.org/draft/2020-12/schema"


class InvalidSchemaJSON(ValueError):
    """The document is not acceptable as strict, unambiguous schema JSON."""


class UnsafeSchemaReferences(ValueError):
    """The schema reference graph is not closed, acyclic, and evaluable."""


def _object_without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidSchemaJSON
        result[key] = value
    return result


def _reject_nonfinite_number(_value: str) -> None:
    raise InvalidSchemaJSON


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise InvalidSchemaJSON
    return parsed


def parse_schema_json(encoded: str) -> object:
    """Decode strict JSON, rejecting duplicate keys and non-finite numbers."""
    try:
        return json.loads(
            encoded,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_nonfinite_number,
            parse_float=_finite_float,
        )
    except (RecursionError, ValueError) as error:
        raise InvalidSchemaJSON from error


def _pointer_target(root: Mapping[str, Any], reference: str) -> object:
    if reference != "#" and not reference.startswith("#/"):
        raise UnsafeSchemaReferences
    fragment = reference[1:]
    if re.search(r"%(?![0-9A-Fa-f]{2})", fragment):
        raise UnsafeSchemaReferences
    try:
        decoded = unquote_to_bytes(fragment).decode("utf-8")
    except UnicodeError as error:
        raise UnsafeSchemaReferences from error
    if not decoded:
        return root

    target: object = root
    for encoded_token in decoded[1:].split("/"):
        if re.search(r"~(?:[^01]|$)", encoded_token):
            raise UnsafeSchemaReferences
        token = encoded_token.replace("~1", "/").replace("~0", "~")
        if isinstance(target, dict):
            if token not in target:
                raise UnsafeSchemaReferences
            target = target[token]
        elif isinstance(target, list):
            if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
                raise UnsafeSchemaReferences
            position = int(token)
            if position >= len(target):
                raise UnsafeSchemaReferences
            target = target[position]
        else:
            raise UnsafeSchemaReferences
    return target


def validate_closed_schema_references(schema: Mapping[str, Any]) -> None:
    """Require every recognized reference branch to be closed and acyclic.

    Draft 2020-12 permits resource graphs, anchors, and dynamic references.
    Palomar's two current repository schemas need none of them: their reusable
    definitions are all local JSON-Pointer targets. Keeping that exact smaller
    contract makes the policy graph decidable without retrieval and lets an
    empty registry reject a bad branch just as a populated one does.
    """
    active: set[int] = set()
    complete: set[int] = set()
    checked_targets: set[int] = set()

    def visit(node: object, *, root: bool = False) -> None:
        if not isinstance(node, (dict, bool)):
            raise UnsafeSchemaReferences
        marker = id(node)
        if marker in active:
            raise UnsafeSchemaReferences
        if marker in complete:
            return
        active.add(marker)
        if isinstance(node, dict):
            if root:
                declared_draft = node.get("$schema")
                if declared_draft is not None and declared_draft != DRAFT202012_URI:
                    raise UnsafeSchemaReferences
            elif "$id" in node or "$schema" in node:
                raise UnsafeSchemaReferences
            if any(
                keyword in node
                for keyword in ("$anchor", "$dynamicAnchor", "$dynamicRef")
            ):
                raise UnsafeSchemaReferences
            if "$ref" in node:
                reference = node["$ref"]
                if not isinstance(reference, str):
                    raise UnsafeSchemaReferences
                target = _pointer_target(schema, reference)
                if not isinstance(target, (dict, bool)):
                    raise UnsafeSchemaReferences
                target_marker = id(target)
                if target_marker not in complete and target_marker not in checked_targets:
                    try:
                        jsonschema.Draft202012Validator.check_schema(target)
                    except (jsonschema.SchemaError, RecursionError) as error:
                        raise UnsafeSchemaReferences from error
                    checked_targets.add(target_marker)
                visit(target)
            for child in DRAFT202012.subresources_of(node):
                visit(child)
        active.remove(marker)
        complete.add(marker)

    try:
        visit(schema, root=True)
    except UnsafeSchemaReferences:
        raise
    except Exception as error:
        # Policy is repository input. A library implementation detail must not
        # turn malformed policy into a traceback or a conditionally green run.
        raise UnsafeSchemaReferences from error
