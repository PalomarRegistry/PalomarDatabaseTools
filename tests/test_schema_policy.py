"""Direct tests for strict repository-schema policy."""

from __future__ import annotations

import pytest
from schema_policy import (
    InvalidSchemaJSON,
    UnsafeSchemaReferences,
    parse_schema_json,
    validate_closed_schema_references,
)


@pytest.mark.parametrize(
    "encoded",
    [
        '{"nested": {"name": 1, "name": 2}}',
        '{"value": NaN}',
        '{"value": Infinity}',
        '{"value": -Infinity}',
        '{"value": 1e999}',
    ],
)
def test_schema_json_is_strict_and_unambiguous(encoded):
    with pytest.raises(InvalidSchemaJSON):
        parse_schema_json(encoded)


def test_local_acyclic_json_pointer_references_are_the_closed_contract():
    validate_closed_schema_references(
        {
            "$defs": {"word": {"type": "string"}},
            "properties": {"name": {"$ref": "#/$defs/word"}},
        }
    )


def test_boolean_defs_chains_and_encoded_pointers_are_supported():
    validate_closed_schema_references(
        {
            "$defs": {
                "word value": {"type": "string"},
                "alias": {"$ref": "#/$defs/word%20value"},
            },
            "properties": {"name": {"$ref": "#/$defs/alias"}},
            "not": True,
        }
    )


def test_array_index_pointer_targets_are_supported():
    validate_closed_schema_references(
        {
            "prefixItems": [{"type": "string"}],
            "contains": {"$ref": "#/prefixItems/0"},
        }
    )


def test_ref_like_data_inside_an_annotation_is_not_schema_policy():
    validate_closed_schema_references(
        {
            "type": "object",
            "examples": [{"$ref": "https://example.invalid/annotation"}],
        }
    )


@pytest.mark.parametrize(
    "schema",
    [
        {"properties": {"unused": {"$ref": "https://example.invalid/policy"}}},
        {"properties": {"unused": {"$ref": "#/$defs/missing"}}},
        {"title": "not a schema", "$ref": "#/title"},
        {"required": ["name"], "$ref": "#/required"},
        {"$ref": "#"},
        {"$defs": {"a": {"$ref": "#/$defs/b"}, "b": {"$ref": "#/$defs/a"}}},
        {"properties": {"nested": {"$id": "nested.json", "type": "string"}}},
        {
            "properties": {
                "nested": {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "type": "string",
                }
            }
        },
        {"$schema": "http://json-schema.org/draft-07/schema#", "type": "string"},
        {"$anchor": "local", "type": "string"},
        {"$dynamicAnchor": "dynamic", "type": "string"},
        {"$dynamicRef": "#dynamic"},
    ],
)
def test_open_recursive_or_unevaluable_reference_graphs_are_rejected(schema):
    with pytest.raises(UnsafeSchemaReferences):
        validate_closed_schema_references(schema)
