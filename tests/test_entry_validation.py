"""Direct tests for the canonical-entry validation boundary."""

from __future__ import annotations

import copy
import json
import pathlib
import shutil
import subprocess
import sys
import urllib.request

import pytest
import validate as database_validation
from entry_validation import (
    ENTRY_SCHEMA_EVALUATION_ERROR,
    ENTRY_SCHEMA_NAME,
    ENTRY_SCHEMA_VERSION,
    EntrySchemaUnevaluable,
    entry_consistency_errors,
    entry_schema_violations,
    load_entry_schema,
    preservation_errors,
)
from validate import validate


ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_the_sole_schema_and_fixture_agree_at_the_boundary():
    validator, errors = load_entry_schema(ROOT)
    entry = json.loads((ROOT / "tests/fixtures/entry.json").read_text())
    schema = json.loads((ROOT / ENTRY_SCHEMA_NAME).read_text())

    assert errors == []
    assert validator is not None
    assert entry_schema_violations(validator, entry) == []
    assert schema["properties"]["schema_version"]["const"] == ENTRY_SCHEMA_VERSION


def test_schema_discovery_rejects_an_alternate_entry_contract(db):
    db.write_json("schema-v99.json", {})

    validator, errors = load_entry_schema(db.path)

    assert validator is not None
    assert errors == [
        "schema-v99.json: unsupported entry schema document; "
        "schema-v3.json is the sole current entry contract"
    ]


def test_schema_discovery_fails_closed_when_the_sole_contract_is_missing(db):
    (db.path / ENTRY_SCHEMA_NAME).unlink()

    validator, errors = load_entry_schema(db.path)

    assert validator is None
    assert errors == [
        f"{ENTRY_SCHEMA_NAME}: the sole entry schema is missing or symbolic"
    ]


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        ("{\n", "schema-v3.json: entry schema is not valid JSON"),
        (
            '{"type": "object", "type": "string"}\n',
            "schema-v3.json: entry schema is not valid JSON",
        ),
        ('{"minimum": NaN}\n', "schema-v3.json: entry schema is not valid JSON"),
        ('{"minimum": Infinity}\n', "schema-v3.json: entry schema is not valid JSON"),
        ('{"minimum": -Infinity}\n', "schema-v3.json: entry schema is not valid JSON"),
        ('{"minimum": 1e999}\n', "schema-v3.json: entry schema is not valid JSON"),
        ("[]\n", "schema-v3.json: entry schema must be a JSON object"),
        ("true\n", "schema-v3.json: entry schema must be a JSON object"),
        (
            '{"type": 7}\n',
            "schema-v3.json: entry schema is not valid Draft 2020-12 JSON Schema",
        ),
        (
            '{"type": "string", "pattern": "["}\n',
            "schema-v3.json: entry schema is not valid Draft 2020-12 JSON Schema",
        ),
    ],
)
def test_broken_entry_schema_forms_are_closed_errors(db, contents, expected):
    db.write(ENTRY_SCHEMA_NAME, contents)

    validator, errors = load_entry_schema(db.path)

    assert validator is None
    assert errors == [expected]


def test_an_unreadable_entry_schema_is_a_deterministic_error(
    db, monkeypatch
):
    schema = db.path / ENTRY_SCHEMA_NAME
    read_text = pathlib.Path.read_text

    def refuse_schema(path, *args, **kwargs):
        if path == schema:
            raise PermissionError("host-specific detail must not escape")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", refuse_schema)

    assert load_entry_schema(db.path) == (
        None,
        ["schema-v3.json: entry schema cannot be read"],
    )


def test_non_utf8_entry_schema_is_a_closed_read_error(db):
    (db.path / ENTRY_SCHEMA_NAME).write_bytes(b"\xff\xfe")

    assert load_entry_schema(db.path) == (
        None,
        ["schema-v3.json: entry schema is not valid UTF-8"],
    )


def test_alternate_schema_errors_stay_before_the_canonical_schema_error(db):
    db.write_json("schema-v99.json", {})
    db.write(ENTRY_SCHEMA_NAME, "{\n")

    validator, errors = load_entry_schema(db.path)

    assert validator is None
    assert errors == [
        "schema-v99.json: unsupported entry schema document; "
        "schema-v3.json is the sole current entry contract",
        "schema-v3.json: entry schema is not valid JSON",
    ]


def test_a_live_symbolic_entry_schema_is_not_loaded(db):
    schema = db.path / ENTRY_SCHEMA_NAME
    schema.rename(db.path / "entry-schema-elsewhere.json")
    schema.symlink_to("entry-schema-elsewhere.json")

    validator, errors = load_entry_schema(db.path)

    assert validator is None
    assert errors == [
        "schema-v3.json: the sole entry schema is missing or symbolic"
    ]


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "#/$defs/missing"},
        {"$ref": "https://example.invalid/hostile-entry-schema.json"},
        {"$ref": "#"},
        {"title": "scalar target", "$ref": "#/title"},
        {"required": ["id"], "$ref": "#/required"},
        {
            "properties": {
                "unused": {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "dependencies": {
                        "name": {"$ref": "#/$defs/missing"}
                    },
                }
            }
        },
    ],
)
def test_hostile_entry_schema_evaluation_never_escapes_or_fetches(
    db, schema, monkeypatch
):
    def forbid_network(*_args, **_kwargs):
        raise AssertionError("entry schema validation attempted network retrieval")

    monkeypatch.setattr(urllib.request, "urlopen", forbid_network)
    db.write_json(
        ENTRY_SCHEMA_NAME,
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            **schema,
        },
    )
    assert load_entry_schema(db.path) == (
        None,
        [ENTRY_SCHEMA_EVALUATION_ERROR],
    )


def test_an_unused_external_reference_is_red_in_an_empty_registry(db):
    for directory in ("entries", "evidence", "renders", "scores"):
        shutil.rmtree(db.path / directory)
        (db.path / directory).mkdir()
    db.reindex()
    schema = db.read_json(ENTRY_SCHEMA_NAME)
    schema["properties"]["unused_future_field"] = {
        "$ref": "https://example.invalid/unused-entry-policy.json"
    }
    db.write_json(ENTRY_SCHEMA_NAME, schema)

    assert validate(db.path) == [ENTRY_SCHEMA_EVALUATION_ERROR]


def test_the_pure_evaluator_translates_an_unexpected_library_failure():
    class BrokenValidator:
        def iter_errors(self, _entry):
            raise AttributeError("library implementation detail")

    with pytest.raises(EntrySchemaUnevaluable):
        entry_schema_violations(BrokenValidator(), {})


def test_runtime_schema_failure_is_once_only_and_keeps_other_checks(
    db, monkeypatch
):
    db.add_entry("PALOMAR-2026-07-29-000002", 1)
    entry = db.read_json("entries/PALOMAR-2026-07-29-000001-v1.json")
    db.rename_entry(entry, "entries/PALOMAR-2026-07-29-000001-v9.json")

    def fail_evaluation(_validator, _entry):
        raise EntrySchemaUnevaluable

    monkeypatch.setattr(
        database_validation,
        "entry_schema_violations",
        fail_evaluation,
    )

    errors = validate(db.path)

    assert errors.count(ENTRY_SCHEMA_EVALUATION_ERROR) == 1
    assert (
        "entries/PALOMAR-2026-07-29-000001-v9.json: filename must be "
        "PALOMAR-2026-07-29-000001-v1.json"
    ) in errors


def test_an_unevaluable_schema_precedes_but_keeps_independent_entry_errors(db):
    entry = db.read_json("entries/PALOMAR-2026-07-29-000001-v1.json")
    db.rename_entry(entry, "entries/PALOMAR-2026-07-29-000001-v9.json")
    db.write_json(
        ENTRY_SCHEMA_NAME,
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$ref": "#",
        },
    )

    errors = validate(db.path)

    assert errors[:2] == [
        ENTRY_SCHEMA_EVALUATION_ERROR,
        "entries/PALOMAR-2026-07-29-000001-v9.json: filename must be "
        "PALOMAR-2026-07-29-000001-v1.json",
    ]


def test_the_full_cli_reports_a_malformed_entry_schema_without_a_traceback(db):
    db.write(ENTRY_SCHEMA_NAME, "{\n")

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "validate.py"),
            "--root",
            str(db.path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert result.stderr.splitlines() == [
        "schema-v3.json: entry schema is not valid JSON"
    ]
    assert "Traceback" not in result.stdout + result.stderr


def test_the_loader_reports_one_recursive_schema_error_for_multiple_entries(db):
    db.add_entry("PALOMAR-2026-07-29-000002", 1)
    db.write_json(
        ENTRY_SCHEMA_NAME,
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$ref": "#",
        },
    )

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "validate.py"),
            "--root",
            str(db.path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert result.stderr.splitlines() == [ENTRY_SCHEMA_EVALUATION_ERROR]
    assert "Traceback" not in result.stdout + result.stderr


def test_entry_consistency_is_pure_and_reports_derived_urls(db):
    entry = db.entry_data("PALOMAR-2026-07-29-000002", 1)
    db.install_entry(entry)
    before = copy.deepcopy(entry)

    assert entry_consistency_errors("entry.json", entry) == []
    assert entry == before

    entry["source"]["tree_url"] = "https://github.com/somebody/else/tree/main"
    assert any(
        error.startswith("entry.json:source.tree_url: must be ")
        for error in entry_consistency_errors("entry.json", entry)
    )


def test_preservation_order_is_checked_without_validation_orchestration(db):
    entry = db.entry_data("PALOMAR-2026-07-29-000002", 1)
    db.install_entry(entry)
    entry["preservation"]["repositories"].reverse()

    assert preservation_errors("entry.json", entry) == [
        "entry.json:preservation.repositories: must exactly cover source, Git dependencies, "
        "and substantive formalization in canonical order"
    ]
