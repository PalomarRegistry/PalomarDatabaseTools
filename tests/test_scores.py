"""The scores are recorded beside the database, and never published.

They used to sit inside the record, and the publisher removed them on the way
out. That made a published record a *projection*: its bytes were a function of
publisher code rather than of the commit, and that code changed shape twice
under a fixed record. Nothing that is served can be treated as immutable, or
cached, while that is true.

So the record is now published exactly as committed, and anything the public
must not see has to be somewhere the publisher never looks. `scores/` is that
place. It is frozen like `entries/`, because the decision has to stay
reconstructable, and it is bound to the review it explains, because otherwise a
second pass could leave the first pass's numbers in place and nothing would say
so.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest
import validate as validate_module
import validation_scope
from check_append_only import check
from conftest import SAMPLE_SCORES
from validate import validate

ENTRY = "PALOMAR-2026-07-29-000001-v1.json"
ROOT = pathlib.Path(__file__).resolve().parents[1]


def violations(repo) -> list[str]:
    base = repo.git("rev-parse", "HEAD").strip()
    head = repo.commit("change under test")
    return check(repo.path, base, head)


# What the validator requires.


def test_a_registered_version_without_its_scores_is_rejected(db):
    """Not recording them would make the decision unreconstructable, and the
    record carries no trace of the numbers to fall back on."""
    (db.path / "scores" / ENTRY).unlink()
    errors = validate(db.path)
    assert any(f"scores/{ENTRY}: missing" in error for error in errors)


def test_scores_for_no_registered_version_are_rejected(db):
    db.write_json("scores/PALOMAR-2026-07-29-000404-v1.json", {"schema_version": 1})
    errors = validate(db.path)
    assert any("not recorded for any registered version" in error for error in errors)


def test_scores_are_checked_against_their_schema(db):
    scores = db.read_json(f"scores/{ENTRY}")
    scores["scores"]["clarity"] = 6
    db.write_json(f"scores/{ENTRY}", scores)
    errors = validate(db.path)
    assert any("scores.clarity" in error for error in errors)


def test_an_unknown_field_in_the_scores_is_rejected(db):
    """The file exists to hold the numbers, not to become a second record."""
    scores = db.read_json(f"scores/{ENTRY}")
    scores["reviewer_notes"] = "something private that belongs in the review"
    db.write_json(f"scores/{ENTRY}", scores)
    errors = validate(db.path)
    assert any("reviewer_notes" in error for error in errors)


@pytest.mark.parametrize(
    ("field", "value"),
    [("reviewed_at", "2030-01-01T00:00:00Z"), ("policy_commit", "f" * 40)],
)
def test_scores_must_belong_to_the_review_they_explain(db, field, value):
    """Without this, a re-review could register a new verdict while the scores
    beside it still describe the pass before, and the record would look fine."""
    scores = db.read_json(f"scores/{ENTRY}")
    scores[field] = value
    db.write_json(f"scores/{ENTRY}", scores)
    errors = validate(db.path)
    assert any(f"scores/{ENTRY}:{field}" in error for error in errors)


def test_scores_must_be_ordinary_files(db):
    """A symlink freezes the target string, not the bytes read through it."""
    target = db.path / "scores" / ENTRY
    contents = target.read_bytes()
    target.unlink()
    db.write("elsewhere.json", contents.decode())
    target.symlink_to("../elsewhere.json")
    errors = validate(db.path)
    assert any("must be ordinary files, not symbolic links" in error for error in errors)


def test_a_database_that_records_its_scores_is_valid(db):
    assert [error for error in validate(db.path) if "scores" in error] == []


def test_the_orchestrator_passes_only_entries_and_the_frozen_scope(
    db, monkeypatch
):
    seen = []

    def fake_validate_scores(root, entries, frozen_paths):
        seen.append((root, entries, frozen_paths))
        return ["score boundary marker"]

    monkeypatch.setattr(
        validate_module.score_validation, "validate_scores", fake_validate_scores
    )
    frozen = frozenset({f"scores/{ENTRY}"})
    scope = validation_scope.ValidationScope(
        frozenset(), frozen, "HEAD", frozenset()
    )

    errors = validate_module.validate(db.path, scope)

    assert errors == ["score boundary marker"]
    assert seen == [
        (
            db.path,
            [],
            frozen,
        )
    ]


def test_the_full_cli_reports_a_broken_score_schema_without_a_traceback(db):
    db.write("scores-v1.json", "{\n")

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
        "scores-v1.json: score schema is not valid JSON"
    ]
    assert "Traceback" not in result.stdout + result.stderr


# What the record may no longer contain.


def test_the_record_may_not_carry_the_scores_back(db):
    """`review` is `additionalProperties: false`, so the schema is the guard.

    While the publisher did the removing, forgetting to call it was enough to
    publish the numbers. Now nothing can be forgotten: a record carrying them
    is not a valid record.
    """
    entry = db.read_json(f"entries/{ENTRY}")
    entry["review"]["scores"] = dict(SAMPLE_SCORES)
    db.write_json(f"entries/{ENTRY}", entry)
    errors = validate(db.path)
    assert any("review" in error and "scores" in error for error in errors)


# What a commit may do to them.


def test_recorded_scores_are_immutable(repo):
    scores = repo.read_json(f"scores/{ENTRY}")
    scores["scores"]["clarity"] = 3
    repo.write_json(f"scores/{ENTRY}", scores)
    assert any("immutable, byte for byte" in error for error in violations(repo))


def test_recorded_scores_are_never_deleted(repo):
    (repo.path / "scores" / ENTRY).unlink()
    assert any("never deleted or renamed" in error for error in violations(repo))


def test_recording_the_scores_of_a_new_version_is_permitted(repo):
    repo.add_entry("PALOMAR-2026-07-29-000002", 1)
    assert violations(repo) == []


def test_scores_may_not_become_executable(repo):
    (repo.path / "scores" / ENTRY).chmod(0o755)
    assert any("file mode of recorded review scores" in error for error in violations(repo))


# What is served.


def test_the_scores_never_reach_the_release(db, tmp_path):
    from stage_public import stage_public

    output = tmp_path / "release"
    stage_public(db.path, output)
    body = "\n".join(
        path.read_text(errors="ignore")
        for path in output.rglob("*")
        if path.is_file() and path.suffix in (".json", ".xml")
    )
    for axis in SAMPLE_SCORES:
        assert axis not in body, axis
    assert json.loads((output / "index.json").read_text())["entries"], "nothing was staged"
