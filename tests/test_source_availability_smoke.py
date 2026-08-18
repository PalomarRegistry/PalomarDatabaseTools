"""The availability smoke test follows the entry and score schemas it checks."""

import entry_validation
import smoke_source_availability
import score_validation

from smoke_source_availability import _schema_formats


def test_schema_formats_recurse_and_ignore_non_string_annotations():
    schema = {
        "properties": {"first_registered_on": {"format": "date"}},
        "allOf": [
            {"items": {"format": "uri"}},
            {"properties": {"renderer": {"format": {"const": "verso-html"}}}},
        ],
    }

    assert _schema_formats(schema) == {"date", "uri"}


def test_score_schema_loader_errors_are_reported_without_a_second_parser(
    monkeypatch, capsys
):
    assert (
        smoke_source_availability.load_score_schema
        is score_validation.load_score_schema
    )
    error = "scores-v1.json: score schema is not valid JSON"
    monkeypatch.setattr(
        smoke_source_availability,
        "load_score_schema",
        lambda _root, *, required: (None, [error]),
    )

    assert smoke_source_availability.main() == 1
    captured = capsys.readouterr()
    assert captured.err.splitlines() == [error]


def test_entry_schema_loader_errors_are_reported_without_a_second_parser(
    monkeypatch, capsys
):
    assert (
        smoke_source_availability.load_entry_schema
        is entry_validation.load_entry_schema
    )
    error = "schema-v3.json: entry schema is not valid JSON"
    monkeypatch.setattr(
        smoke_source_availability,
        "load_entry_schema",
        lambda _root: (None, [error]),
    )

    assert smoke_source_availability.main() == 1
    captured = capsys.readouterr()
    assert captured.err.splitlines() == [error]


def test_runtime_smoke_rejects_an_unused_external_entry_reference(
    db, monkeypatch, capsys
):
    schema = db.read_json("schema-v3.json")
    schema["properties"]["unused_future_field"] = {
        "$ref": "https://example.invalid/unused-entry-policy.json"
    }
    db.write_json("schema-v3.json", schema)
    monkeypatch.setattr(smoke_source_availability, "ROOT", db.path)

    assert smoke_source_availability.main() == 1
    captured = capsys.readouterr()
    assert captured.err.splitlines() == [
        "schema-v3.json: entry schema cannot be evaluated safely"
    ]
