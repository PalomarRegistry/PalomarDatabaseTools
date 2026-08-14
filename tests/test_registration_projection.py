"""Closed-contract tests for the segmented registration authority."""

from __future__ import annotations

import json

import pytest

import registration_projection
from validate import validate
from validation_scope import scope_of


FIRST = "PALOMAR-2026-07-29-000001"


def _authority_paths(db) -> list[str]:
    submission_id = db.entry_data(FIRST, 1)["submission"]["submission_id"]
    identity = registration_projection._identity(db.entry_data(FIRST, 1))
    return [
        f"registrations/results/{FIRST}.json",
        f"registrations/submissions/{submission_id}.json",
        "registrations/days/2026-07-29.json",
        registration_projection.identity_path(identity),
    ]


def test_an_empty_authority_needs_no_untrackable_empty_directories(tmp_path):
    assert registration_projection.all_result_documents(tmp_path) == []
    assert registration_projection.validate_projections(tmp_path, []) == []


def test_full_validation_rejects_a_gap_in_one_days_allocations(db):
    db.add_entry("PALOMAR-2026-07-29-000003", 1)

    assert any(
        "result serials are not contiguous from 1" in error
        for error in validate(db.path)
    )


def test_full_validation_rejects_two_ids_for_one_stable_identity(db):
    second = db.entry_data("PALOMAR-2026-07-29-000002", 1)
    second["formalization"]["comparator_config_path"] = "comparator.json"
    db.install_entry(second)

    assert any(
        "registration identity belongs to two results" in error
        for error in validate(db.path)
    )


@pytest.mark.parametrize(
    "encoded",
    [
        '{"schema_version":1,"schema_version":1}\n',
        '{"schema_version":NaN}\n',
        '{"schema_version":Infinity}\n',
        '{"schema_version":-Infinity}\n',
        '{"schema_version":1e400}\n',
    ],
)
def test_current_projection_json_is_strict(db, encoded):
    relative = f"registrations/results/{FIRST}.json"
    db.write(relative, encoded)

    assert any("is not valid strict JSON" in error for error in validate(db.path))


@pytest.mark.parametrize("projection", range(4))
def test_current_projection_must_not_be_executable(db, projection):
    relative = _authority_paths(db)[projection]
    (db.path / relative).chmod(0o755)

    assert any(
        relative in error and "non-executable ordinary file" in error
        for error in validate(db.path)
    )


@pytest.mark.parametrize("mode", [0o600, 0o640, 0o644, 0o664])
def test_harmless_worktree_umask_permissions_do_not_change_git_mode(db, mode):
    (db.path / _authority_paths(db)[0]).chmod(mode)

    assert validate(db.path) == []


@pytest.mark.parametrize("projection", range(4))
def test_validated_base_projection_requires_git_mode_100644(repo, projection):
    relative = _authority_paths(repo)[projection]
    (repo.path / relative).chmod(0o755)
    base = repo.commit("an executable authority document")

    with pytest.raises(ValueError, match="must be an ordinary mode 100644 file"):
        registration_projection._git_blob(repo.path, base, relative)


def test_full_validation_closes_the_registration_authority_top_level(db):
    db.write("registrations/notes.json", "{}\n")

    assert any(
        error == "registrations/notes.json: unexpected authority path"
        for error in validate(db.path)
    )


def test_identity_authority_namespace_is_closed_by_the_current_contract(db):
    db.write("registrations/identities/future.json", "{}\n")

    assert any(
        error
        == "registrations/identities/future.json: unexpected registration projection path"
        for error in validate(db.path)
    )


@pytest.mark.parametrize(
    ("field", "value", "complaint"),
    [
        ("title", None, "malformed presentation text"),
        ("classification", {"arxiv": [1], "msc2020": []}, "malformed classification"),
    ],
)
def test_result_presentation_rows_have_closed_types(db, field, value, complaint):
    relative = f"registrations/results/{FIRST}.json"
    result = db.read_json(relative)
    result["versions"][0][field] = value
    db.write_json(relative, result)

    with pytest.raises(ValueError, match=complaint):
        registration_projection.load_result(db.path, FIRST)


def test_projection_dates_must_be_real_calendar_dates():
    with pytest.raises(ValueError, match="date disagrees with its path"):
        registration_projection._validate_day_shape(
            {"schema_version": 1, "date": "2026-02-30", "last_serial": 1},
            "2026-02-30",
            "registrations/days/2026-02-30.json",
        )
    result = {
        "schema_version": 1,
        "id": "PALOMAR-2026-02-30-000001",
        "accepted_at": "2026-02-30",
        "identity": {
            "source_repository": "https://example.invalid/source",
            "project_path": None,
            "comparator_config_path": "comparator.json",
        },
        "versions": [],
    }
    with pytest.raises(ValueError, match="accepted_at disagrees with id"):
        registration_projection._validate_result_shape(
            result, result["id"], "registrations/results/impossible.json"
        )


@pytest.mark.parametrize("malformation", ["duplicate", "nonfinite"])
def test_scoped_validation_rejects_ambiguous_json_in_the_validated_base(
    repo, malformation
):
    relative = f"registrations/results/{FIRST}.json"
    encoded = json.dumps(repo.read_json(relative), indent=2, sort_keys=True) + "\n"
    if malformation == "duplicate":
        encoded = encoded.replace(
            '"schema_version": 1',
            '"schema_version": 1,\n  "schema_version": 1',
            1,
        )
    else:
        encoded = encoded.replace('"schema_version": 1', '"schema_version": NaN', 1)
    repo.write(relative, encoded)
    base = repo.commit("an ambiguous projection in the claimed validated base")
    repo.add_entry(FIRST, 2)
    repo.commit("append a version")

    scope = scope_of(repo.path, base)
    assert scope is not None
    assert any("at validated base: is not valid strict JSON" in error for error in validate(repo.path, scope))


def test_a_missing_base_path_is_distinct_from_an_unresolvable_base(repo):
    base = repo.commit("a valid base")

    assert registration_projection._git_blob(
        repo.path, base, "registrations/results/PALOMAR-2026-07-29-999999.json"
    ) is None
    with pytest.raises(ValueError, match="at validated base cannot be resolved"):
        registration_projection._git_blob(
            repo.path, "not-a-revision", f"registrations/results/{FIRST}.json"
        )


def test_scoped_append_refuses_a_501st_version_at_the_owner_boundary(repo):
    relative = f"registrations/results/{FIRST}.json"
    result = repo.read_json(relative)
    template = result["versions"][0]
    result["versions"] = [
        {
            **template,
            "version": version,
            "submission_id": f"{version:012d}",
            "path": f"entries/{FIRST}-v{version}.json",
        }
        for version in range(1, 501)
    ]
    repo.write_json(relative, result)
    base = repo.commit("a result at the current version cap")
    entry = repo.entry_data(FIRST, 501)

    errors = registration_projection.validate_projections(
        repo.path,
        [(f"entries/{FIRST}-v501.json", entry)],
        base=base,
        changed_paths=frozenset(
            {
                relative,
                registration_projection.submission_path(
                    entry["submission"]["submission_id"]
                ),
            }
        ),
    )

    assert errors == [
        f"{relative}: has more than 500 versions, "
        "which one result projection may not carry"
    ]


@pytest.mark.parametrize("same_result", [True, False])
def test_one_delta_cannot_bind_a_submission_id_to_two_versions(repo, same_result):
    base = repo.commit("a valid base")
    if same_result:
        entries = [repo.entry_data(FIRST, version) for version in (2, 3)]
    else:
        entries = [
            repo.entry_data("PALOMAR-2026-07-29-000002", 1),
            repo.entry_data("PALOMAR-2026-07-29-000003", 1),
        ]
    for entry in entries:
        entry["submission"]["submission_id"] = "sameintake12"
    changed_paths = {"registrations/submissions/sameintake12.json"} | {
        registration_projection.result_path(entry["id"]) for entry in entries
    }
    if not same_result:
        changed_paths.add(registration_projection.day_path("2026-07-29"))
        changed_paths.update(
            registration_projection.identity_path(
                registration_projection._identity(entry)
            )
            for entry in entries
        )

    errors = registration_projection.validate_projections(
        repo.path,
        [
            (f"entries/{entry['id']}-v{entry['version']}.json", entry)
            for entry in entries
        ],
        base=base,
        changed_paths=frozenset(changed_paths),
    )

    assert errors == [
        "registrations/submissions/sameintake12.json: "
        "submission_id is bound to two versions"
    ]


def test_a_retry_identity_cannot_be_rebound_after_its_first_registration(repo):
    base = repo.commit("a registered intake")
    submission_id = repo.entry_data(FIRST, 1)["submission"]["submission_id"]
    entry = repo.entry_data(FIRST, 2)
    entry["submission"]["submission_id"] = submission_id

    errors = registration_projection.validate_projections(
        repo.path,
        [(f"entries/{FIRST}-v2.json", entry)],
        base=base,
        changed_paths=frozenset({registration_projection.result_path(FIRST)}),
    )

    assert errors == [
        f"registrations/submissions/{submission_id}.json: "
        "new submission_id has no new binding or is already bound"
    ]


def test_scoped_transition_requires_result_and_day_projection_changes(repo):
    base = repo.commit("a valid base")
    identifier = "PALOMAR-2026-07-29-000002"
    entry = repo.entry_data(identifier, 1)
    result = registration_projection.result_path(identifier)
    day = registration_projection.day_path("2026-07-29")
    binding = registration_projection.submission_path(
        entry["submission"]["submission_id"]
    )
    identity = registration_projection.identity_path(
        registration_projection._identity(entry)
    )

    missing_result = registration_projection.validate_projections(
        repo.path,
        [(f"entries/{identifier}-v1.json", entry)],
        base=base,
        changed_paths=frozenset({binding, day}),
    )
    assert missing_result == [
        f"{result}: appending a version must change its result projection"
    ]

    missing_day = registration_projection.validate_projections(
        repo.path,
        [(f"entries/{identifier}-v1.json", entry)],
        base=base,
        changed_paths=frozenset({result, binding, identity}),
    )
    assert missing_day == [
        f"{day}: allocating a result must change its day projection"
    ]


def test_scoped_transition_rejects_a_malformed_result_id_before_git_lookup(repo):
    base = repo.commit("a valid base")
    entry = repo.entry_data(FIRST, 2)
    entry["id"] = "../not-a-result"

    assert registration_projection.validate_projections(
        repo.path,
        [("entries/not-a-result-v2.json", entry)],
        base=base,
    ) == ["entries name a malformed result id: '../not-a-result'"]


def test_scoped_transition_validates_the_base_day_at_its_owner(repo):
    relative = "registrations/days/2026-07-29.json"
    day = repo.read_json(relative)
    day["last_serial"] = 0
    repo.write_json(relative, day)
    base = repo.commit("an impossible day counter at the claimed base")
    identifier = "PALOMAR-2026-07-29-000002"
    entry = repo.entry_data(identifier, 1)

    errors = registration_projection.validate_projections(
        repo.path,
        [(f"entries/{identifier}-v1.json", entry)],
        base=base,
        changed_paths=frozenset(
            {
                registration_projection.result_path(identifier),
                registration_projection.submission_path(
                    entry["submission"]["submission_id"]
                ),
                relative,
                registration_projection.identity_path(
                    registration_projection._identity(entry)
                ),
            }
        ),
    )

    assert errors == [
        f"{relative} at validated base: last_serial is outside the identifier range"
    ]


def test_scoped_projection_validation_reports_a_malformed_entry_version(repo):
    base = repo.commit("a valid base")
    relative = f"entries/PALOMAR-2026-07-29-000002-v1.json"
    repo.add_entry("PALOMAR-2026-07-29-000002", 1)
    entry = repo.read_json(relative)
    entry["version"] = {}
    repo.write_json(relative, entry)
    repo.commit("add an entry whose version is not an integer")

    scope = scope_of(repo.path, base)
    assert scope is not None
    errors = validate(repo.path, scope)

    assert any("version: {} is not of type 'integer'" in error for error in errors)
    assert any(
        "changed projection paths do not exactly match new entries" in error
        for error in errors
    )


def test_registration_identity_casefolds_the_github_repository_key():
    entry = {
        "source": {"repository": "Kim-Em/Example", "project_path": None},
        "formalization": {"comparator_config_path": "comparator.json"},
    }

    assert registration_projection._identity(entry)["source_repository"] == (
        "kim-em/example"
    )
