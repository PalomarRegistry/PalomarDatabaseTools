"""Private takedown-manifest validation."""

from __future__ import annotations

from takedowns import MODERATOR_LOGINS, MODERATORS, moderator_login
from validate import validate


EXPECTED_MODERATORS = {
    "Jeremy Avigad",
    "Matthew Ballard",
    "Jaume de Dios",
    "Nestor Guillen",
    "Bryna Kra",
    "Kim Morrison",
    "Terence Tao",
    "Ravi Vakil",
    "Akshay Venkatesh",
}


def _row(identifier="PALOMAR-2026-07-29-000001", version=1, **overrides):
    row = {
        "id": identifier,
        "version": version,
        "taken_down_at": "2026-08-06T12:00:00Z",
        "authorized_by_login": "avigad",
        "authorization_issue": 101,
        "reason": "Private operational reason.",
    }
    row.update(overrides)
    return row


def test_a_valid_exact_version_takedown_is_accepted(db):
    db.write_json("takedowns.json", {"schema_version": 1, "takedowns": [_row()]})
    assert validate(db.path) == []


def test_the_moderator_roster_is_explicit_and_closed():
    assert MODERATORS == EXPECTED_MODERATORS


def test_takedown_rows_are_closed_sorted_unique_and_referential(db):
    db.add_entry("PALOMAR-2026-07-29-000001", 2)
    db.write_json(
        "takedowns.json",
        {
            "schema_version": 1,
            "takedowns": [
                _row(version=2, extra="not allowed", authorization_issue=102),
                _row(version=1),
                _row(version=1, authorization_issue=103),
                _row("PALOMAR-2026-07-29-999999", 1, authorization_issue=104),
            ],
        },
    )
    errors = validate(db.path)
    assert any("must contain exactly" in error for error in errors)
    assert any("target is duplicated" in error for error in errors)
    assert any("target does not exist" in error for error in errors)
    assert any("rows must be sorted" in error for error in errors)


def test_takedown_reason_and_timestamp_are_strict(db):
    db.write_json(
        "takedowns.json",
        {
            "schema_version": 1,
            "takedowns": [_row(reason="", taken_down_at="2026-02-30T00:00:00Z")],
        },
    )
    errors = validate(db.path)
    assert any("reason: must contain 1 to 4000" in error for error in errors)
    assert any("must be a real UTC timestamp" in error for error in errors)


def test_takedown_authority_is_closed_to_the_moderator_roster(db):
    db.write_json(
        "takedowns.json",
        {
            "schema_version": 1,
            "takedowns": [_row(authorized_by_login="a-helpful-stranger")],
        },
    )
    assert any(
        "authorized_by_login: must be a current Palomar Moderator's GitHub login" in error
        for error in validate(db.path)
    )


def test_a_typed_display_name_is_not_authority_by_itself(db):
    """The old schema's `authorized_by` is a claim, not a binding, and is gone."""
    row = _row()
    del row["authorized_by_login"]
    row["authorized_by"] = "Jeremy Avigad"
    db.write_json("takedowns.json", {"schema_version": 1, "takedowns": [row]})
    errors = validate(db.path)
    assert any("must contain exactly" in error for error in errors)
    assert any("authorized_by_login" in error for error in errors)


def test_the_authorizing_issue_must_be_a_positive_number(db):
    db.write_json(
        "takedowns.json",
        {"schema_version": 1, "takedowns": [_row(authorization_issue="123")]},
    )
    assert any(
        "authorization_issue: must be the positive number" in error
        for error in validate(db.path)
    )


def test_one_issue_cannot_authorize_two_rows(db):
    db.add_entry("PALOMAR-2026-07-29-000001", 2)
    db.write_json(
        "takedowns.json",
        {"schema_version": 1, "takedowns": [_row(version=1), _row(version=2)]},
    )
    assert any(
        "authorization_issue: authorizes more than one row" in error
        for error in validate(db.path)
    )


def test_a_moderator_login_resolves_however_github_spells_it():
    assert moderator_login("AviGad") == "avigad"
    assert moderator_login(" avigad ") == "avigad"
    assert moderator_login("avigad-impostor") is None
    assert moderator_login(None) is None


def test_every_bound_login_names_a_roster_moderator():
    assert set(MODERATOR_LOGINS.values()) <= MODERATORS
    assert all(login == login.casefold() for login in MODERATOR_LOGINS)


def test_takedowns_manifest_is_required(db):
    db.remove("takedowns.json")
    assert any("file is missing" in error for error in validate(db.path))
