"""Direct tests for the private score-document boundary."""

from __future__ import annotations

import copy
import json
import pathlib
import urllib.request

import pytest
import score_validation

ENTRY = "PALOMAR-2026-07-29-000001-v1.json"


def _entries(db):
    return [
        (f"entries/{path.name}", json.loads(path.read_text(encoding="utf-8")))
        for path in sorted((db.path / "entries").iterdir())
    ]


def test_the_canonical_scores_pass_without_mutating_the_entries(db):
    entries = _entries(db)
    before = copy.deepcopy(entries)

    assert score_validation.validate_scores(db.path, entries) == []
    assert entries == before


def test_the_canonical_score_schema_loads_at_its_owner_boundary(db):
    validator, errors = score_validation.load_score_schema(db.path, required=True)

    assert errors == []
    assert validator is not None


def test_the_score_path_contract_is_owned_here():
    assert score_validation.SCORE_PATH_RE.fullmatch(f"scores/{ENTRY}")
    assert not score_validation.SCORE_PATH_RE.fullmatch(f"scores/nested/{ENTRY}")
    assert not score_validation.SCORE_PATH_RE.fullmatch("scores/not-a-score.json")


def test_a_missing_score_stays_red_in_a_scoped_run(db):
    (db.path / "scores" / ENTRY).unlink()

    assert score_validation.validate_scores(db.path, _entries(db), frozenset()) == [
        f"scores/{ENTRY}: missing, but entries/{ENTRY} is registered. "
        "Every registered version records the scores that decided it."
    ]


def test_a_changed_orphan_score_is_rejected_without_scanning_history(db):
    orphan = "PALOMAR-2026-07-29-000404-v1.json"
    relative = f"scores/{orphan}"
    (db.path / "scores" / ENTRY).write_text("{\n", encoding="utf-8")
    db.write_json(relative, {"schema_version": 1})

    assert score_validation.validate_scores(
        db.path, _entries(db), frozenset({relative})
    ) == [
        f"scores/{orphan}: scores are not recorded for any registered version"
    ]


def test_scores_directory_shape_failure_precedes_the_missing_score_sweep(db):
    scores = db.path / "scores"
    scores.rename(db.path / "scores-backup")
    scores.write_text("not a directory\n", encoding="utf-8")

    assert score_validation.validate_scores(db.path, _entries(db)) == [
        "scores/: must be a directory"
    ]


def test_unchanged_score_bytes_are_not_reopened_in_a_scoped_run(db):
    (db.path / "scores" / ENTRY).write_text("{\n", encoding="utf-8")

    assert score_validation.validate_scores(db.path, _entries(db), frozenset()) == []
    assert any(
        "invalid JSON" in error
        for error in score_validation.validate_scores(db.path, _entries(db))
    )


def test_the_score_schema_is_applied_at_the_boundary(db):
    scores = db.read_json(f"scores/{ENTRY}")
    scores["scores"]["clarity"] = 6
    db.write_json(f"scores/{ENTRY}", scores)

    errors = score_validation.validate_scores(db.path, _entries(db))

    assert any("scores.clarity" in error for error in errors)


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        ("{\n", "scores-v1.json: score schema is not valid JSON"),
        (
            '{"type": "object", "type": "string"}\n',
            "scores-v1.json: score schema is not valid JSON",
        ),
        ('{"minimum": NaN}\n', "scores-v1.json: score schema is not valid JSON"),
        ('{"minimum": Infinity}\n', "scores-v1.json: score schema is not valid JSON"),
        ('{"minimum": -Infinity}\n', "scores-v1.json: score schema is not valid JSON"),
        ("[]\n", "scores-v1.json: score schema must be a JSON object"),
        ("true\n", "scores-v1.json: score schema must be a JSON object"),
        (
            '{"type": 7}\n',
            "scores-v1.json: score schema is not valid Draft 2020-12 JSON Schema",
        ),
    ],
)
def test_broken_score_schema_forms_are_closed_errors(db, contents, expected):
    db.write("scores-v1.json", contents)

    validator, load_errors = score_validation.load_score_schema(
        db.path, required=True
    )

    assert validator is None
    assert load_errors == [expected]
    assert score_validation.validate_scores(db.path, _entries(db)) == [expected]


def test_an_unreadable_score_schema_is_a_deterministic_error(
    db, monkeypatch
):
    entries = _entries(db)
    schema = db.path / score_validation.SCORES_SCHEMA_NAME
    read_text = pathlib.Path.read_text

    def refuse_schema(path, *args, **kwargs):
        if path == schema:
            raise PermissionError("host-specific detail must not escape")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", refuse_schema)
    expected = "scores-v1.json: score schema cannot be read"

    assert score_validation.validate_scores(db.path, entries) == [expected]
    assert score_validation.validate_scores(db.path, entries) == [expected]


def test_non_utf8_score_schema_is_a_closed_read_error(db):
    (db.path / score_validation.SCORES_SCHEMA_NAME).write_bytes(b"\xff\xfe")

    assert score_validation.validate_scores(db.path, _entries(db)) == [
        "scores-v1.json: score schema is not valid UTF-8"
    ]


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        (
            {"$ref": "#/$defs/missing"},
            "scores-v1.json: score schema cannot be evaluated safely",
        ),
        (
            {"$ref": "https://example.invalid/hostile-score-schema.json"},
            "scores-v1.json: score schema cannot be evaluated safely",
        ),
        (
            {"$ref": "#"},
            "scores-v1.json: score schema cannot be evaluated safely",
        ),
        (
            {"title": "scalar target", "$ref": "#/title"},
            "scores-v1.json: score schema cannot be evaluated safely",
        ),
        (
            {"required": ["id"], "$ref": "#/required"},
            "scores-v1.json: score schema cannot be evaluated safely",
        ),
        (
            {"type": "string", "pattern": "["},
            "scores-v1.json: score schema is not valid Draft 2020-12 JSON Schema",
        ),
    ],
)
def test_hostile_schema_evaluation_never_escapes_or_fetches(
    db, schema, expected, monkeypatch
):
    def forbid_network(*_args, **_kwargs):
        raise AssertionError("score schema validation attempted network retrieval")

    monkeypatch.setattr(urllib.request, "urlopen", forbid_network)
    db.add_entry("PALOMAR-2026-07-29-000002", 1)
    db.write_json(
        score_validation.SCORES_SCHEMA_NAME,
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            **schema,
        },
    )

    assert score_validation.load_score_schema(db.path, required=True) == (
        None,
        [expected],
    )
    assert score_validation.validate_scores(db.path, _entries(db)) == [expected]


def test_an_unexpected_score_evaluator_failure_is_closed(
    db, monkeypatch
):
    class BrokenValidator:
        def iter_errors(self, _data):
            raise AttributeError("library implementation detail")

    monkeypatch.setattr(
        score_validation,
        "load_score_schema",
        lambda _root, *, required: (BrokenValidator(), []),
    )

    assert score_validation.validate_scores(db.path, _entries(db)) == [
        score_validation.SCORE_SCHEMA_EVALUATION_ERROR
    ]


def test_a_symbolic_score_schema_is_not_loaded(db):
    schema = db.path / score_validation.SCORES_SCHEMA_NAME
    schema.rename(db.path / "scores-schema-elsewhere.json")
    schema.symlink_to("scores-schema-elsewhere.json")

    validator, errors = score_validation.load_score_schema(db.path, required=True)

    assert validator is None
    assert errors == [
        "scores-v1.json: file is missing, so no scores can be checked"
    ]


def test_an_existing_broken_schema_is_rejected_even_before_the_first_entry(db):
    db.write("scores-v1.json", "{\n")

    validator, errors = score_validation.load_score_schema(db.path, required=False)

    assert validator is None
    assert errors == ["scores-v1.json: score schema is not valid JSON"]


def test_schema_failure_precedes_ordinary_score_binding_errors(db):
    db.write("scores-v1.json", "{\n")
    scores = db.read_json(f"scores/{ENTRY}")
    scores["id"] = "PALOMAR-2026-07-29-000404"
    db.write_json(f"scores/{ENTRY}", scores)

    assert score_validation.validate_scores(db.path, _entries(db)) == [
        "scores-v1.json: score schema is not valid JSON",
        f"scores/{ENTRY}: identity disagrees with its filename",
    ]


def test_scores_are_bound_to_the_review_they_explain(db):
    scores = db.read_json(f"scores/{ENTRY}")
    scores["policy_commit"] = "f" * 40
    db.write_json(f"scores/{ENTRY}", scores)

    errors = score_validation.validate_scores(db.path, _entries(db))

    assert any(
        f"scores/{ENTRY}:policy_commit: must match entries/{ENTRY}" in error
        for error in errors
    )


def test_scores_identity_is_bound_to_the_canonical_filename(db):
    scores = db.read_json(f"scores/{ENTRY}")
    scores["id"] = "PALOMAR-2026-07-29-000404"
    db.write_json(f"scores/{ENTRY}", scores)

    errors = score_validation.validate_scores(db.path, _entries(db))

    assert f"scores/{ENTRY}: identity disagrees with its filename" in errors


def test_the_schema_is_required_when_entries_exist(db):
    (db.path / score_validation.SCORES_SCHEMA_NAME).unlink()

    errors = score_validation.validate_scores(db.path, _entries(db))

    assert errors == [
        "scores-v1.json: file is missing, so no scores can be checked"
    ]


def test_the_schema_is_not_required_without_entries_or_scores(db):
    (db.path / score_validation.SCORES_SCHEMA_NAME).unlink()
    for score in (db.path / "scores").iterdir():
        score.unlink()

    assert score_validation.validate_scores(db.path, []) == []
