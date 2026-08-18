import hashlib
import json

import pytest

import build_registration_lookups as lookups
import patch_surfaces


def identity(
    repository="owner/repository",
    project_path=None,
    comparator_config_path="Comparator/comparator.json",
):
    return {
        "source_repository": repository,
        "project_path": project_path,
        "comparator_config_path": comparator_config_path,
    }


def result(identifier, registration_identity):
    return {"id": identifier, "identity": registration_identity}


def active(identifier, commit="a" * 40):
    return {"id": identifier, "source": {"commit": commit}}


def load(root, relative):
    return json.loads((root / relative).read_text())


def test_identity_digest_has_the_documented_preimage():
    value = identity(project_path="formalization")
    expected = hashlib.sha256(
        b"owner/repository\0formalization\0Comparator/comparator.json"
    ).hexdigest()
    assert lookups.identity_digest(value) == expected


@pytest.mark.parametrize("repository", ["../schema-v3", "owner/..", "./name", "owner/."])
def test_repository_paths_refuse_navigation_segments(repository):
    with pytest.raises(ValueError, match="malformed"):
        lookups.repository_path(repository)


def test_lookup_writes_cannot_escape_the_output(tmp_path):
    with pytest.raises(ValueError, match="outside its output"):
        lookups._write(tmp_path, "repositories/../../elsewhere.json", {})


def test_full_build_writes_repository_and_exact_identity_documents(tmp_path):
    same = identity()
    other = identity(comparator_config_path="Second/comparator.json")
    results = [
        result("PALOMAR-2026-08-12-000001", same),
        result("PALOMAR-2026-08-12-000002", other),
        result("PALOMAR-2026-08-12-000003", same),
    ]

    entries = [
        active("PALOMAR-2026-08-12-000001", "a" * 40),
        active("PALOMAR-2026-08-12-000002", "b" * 40),
        active("PALOMAR-2026-08-12-000003", "c" * 40),
    ]
    lookups.build(
        tmp_path,
        results,
        entries,
        entries,
    )

    repository = load(tmp_path, "repositories/owner/repository.json")
    assert repository == {
        "schema_version": 2,
        "repository": "owner/repository",
        "registrations_total": 3,
        "truncated": False,
        "registrations": [
            {
                "id": "PALOMAR-2026-08-12-000003",
                "project_path": None,
                "comparator_config_path": "Comparator/comparator.json",
            },
            {
                "id": "PALOMAR-2026-08-12-000002",
                "project_path": None,
                "comparator_config_path": "Second/comparator.json",
            },
            {
                "id": "PALOMAR-2026-08-12-000001",
                "project_path": None,
                "comparator_config_path": "Comparator/comparator.json",
            },
        ],
    }
    exact = load(tmp_path, lookups.identity_path(same))
    assert exact["identity"] == same
    assert exact["registration_id"] is None
    assert exact["ambiguous"] is True
    assert exact["schema_version"] == 2
    assert exact["commits"] == ["a" * 40, "c" * 40]


def test_full_build_omits_inactive_registrations_and_caps_repository(tmp_path):
    registrations = [
        result(f"PALOMAR-2026-08-12-{serial:06d}", identity())
        for serial in range(1, lookups.REPOSITORY_LIMIT + 3)
    ]
    active_entries = [active(str(row["id"])) for row in registrations[1:]]
    lookups.build(tmp_path, registrations, active_entries, active_entries)

    document = load(tmp_path, "repositories/owner/repository.json")
    assert document["registrations_total"] == lookups.REPOSITORY_LIMIT + 1
    assert document["truncated"] is True
    assert len(document["registrations"]) == lookups.REPOSITORY_LIMIT
    assert document["registrations"][-1]["id"].endswith("000003")
    assert all(not row["id"].endswith("000001") for row in document["registrations"])


def test_full_build_keeps_withdrawn_commits_for_duplicate_detection(tmp_path):
    identifier = "PALOMAR-2026-08-12-000001"
    same = identity()
    lookups.build(
        tmp_path,
        [result(identifier, same)],
        [active(identifier, "b" * 40)],
        [active(identifier, "a" * 40), active(identifier, "b" * 40)],
    )

    assert load(tmp_path, lookups.identity_path(same))["commits"] == [
        "a" * 40,
        "b" * 40,
    ]


def test_incremental_patch_adds_only_new_registrations(tmp_path):
    prior = tmp_path / "prior"
    output = tmp_path / "output"
    prior.mkdir()
    output.mkdir()
    first = identity()
    lookups.build(
        prior,
        [result("PALOMAR-2026-08-11-000001", first)],
        [active("PALOMAR-2026-08-11-000001", "a" * 40)],
        [active("PALOMAR-2026-08-11-000001", "a" * 40)],
    )

    second = identity(comparator_config_path="Other/comparator.json")
    lookups.patch(
        output,
        prior,
        [("PALOMAR-2026-08-12-000001", second)],
        [("PALOMAR-2026-08-12-000001", second, "b" * 40)],
    )

    repository = load(output, "repositories/owner/repository.json")
    assert repository["registrations_total"] == 2
    assert [row["id"] for row in repository["registrations"]] == [
        "PALOMAR-2026-08-12-000001",
        "PALOMAR-2026-08-11-000001",
    ]
    assert load(output, lookups.identity_path(second))["registration_id"] == (
        "PALOMAR-2026-08-12-000001"
    )


def test_incremental_patch_makes_a_duplicate_identity_ambiguous(tmp_path):
    prior = tmp_path / "prior"
    output = tmp_path / "output"
    prior.mkdir()
    output.mkdir()
    same = identity()
    lookups.build(
        prior,
        [result("PALOMAR-2026-08-11-000001", same)],
        [active("PALOMAR-2026-08-11-000001", "a" * 40)],
        [active("PALOMAR-2026-08-11-000001", "a" * 40)],
    )

    lookups.patch(
        output,
        prior,
        [("PALOMAR-2026-08-12-000001", same)],
        [("PALOMAR-2026-08-12-000001", same, "b" * 40)],
    )

    assert load(output, lookups.identity_path(same))["registration_id"] is None
    assert load(output, lookups.identity_path(same))["ambiguous"] is True


def test_incremental_patch_adds_a_commit_without_readding_the_registration(tmp_path):
    prior = tmp_path / "prior"
    output = tmp_path / "output"
    prior.mkdir()
    output.mkdir()
    same = identity()
    identifier = "PALOMAR-2026-08-11-000001"
    lookups.build(
        prior,
        [result(identifier, same)],
        [active(identifier, "a" * 40)],
        [active(identifier, "a" * 40)],
    )

    lookups.patch(output, prior, [], [(identifier, same, "b" * 40)])

    exact = load(output, lookups.identity_path(same))
    assert exact["registration_id"] == identifier
    assert exact["ambiguous"] is False
    assert exact["commits"] == ["a" * 40, "b" * 40]
    assert not (output / "repositories/owner/repository.json").exists()


def test_malformed_prior_lookup_requests_a_full_rebuild(tmp_path):
    prior = tmp_path / "prior"
    output = tmp_path / "output"
    path = prior / "repositories/owner/repository.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    output.mkdir()

    with pytest.raises(patch_surfaces.Rebuild):
        lookups.patch(
            output,
            prior,
            [("PALOMAR-2026-08-12-000001", identity())],
            [("PALOMAR-2026-08-12-000001", identity(), "a" * 40)],
        )
