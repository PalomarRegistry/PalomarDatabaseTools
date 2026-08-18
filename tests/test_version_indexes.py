"""One document per identifier, naming every version of it being served.

An entry page needs the versions of one result. Reading `index.json` to find
them means fetching the whole registry to render one page: at a hundred
thousand records that is tens of megabytes for four hundred bytes of answer,
and it grows with every result anyone else publishes.

Written at a stable key, so it changes only when that identifier gains or loses
a version. Nothing combines it with another document, so it carries no
generation and needs none.
"""

from __future__ import annotations

import json
import shutil

import pytest
import registration_projection
import release_delta
import stage_public as staging
from conftest import surfaces
from stage_public import FullCheckoutRequired, stage_public

FIRST = "PALOMAR-2026-07-29-000001"
SECOND = "PALOMAR-2026-07-29-000002"


def _versions(site, identifier):
    return json.loads((site / "versions" / f"{identifier}.json").read_text())


def test_every_identifier_gets_a_document_naming_its_versions(db, tmp_path):
    db.add_entry(FIRST, 2)
    db.add_entry(SECOND, 1)
    site = tmp_path / "release"
    stage_public(db.path, site)

    first = _versions(site, FIRST)
    assert first["schema_version"] == 2
    assert first["id"] == FIRST
    assert [row["version"] for row in first["entries"]] == [1, 2]
    assert all(row["id"] == FIRST for row in first["entries"]), "another result leaked in"
    assert [row["version"] for row in _versions(site, SECOND)["entries"]] == [1]


def test_the_rows_are_the_ones_the_index_carries(db, tmp_path):
    """So the existing row validator, path grammar and record URL all apply
    unchanged; only coverage and ordering are new."""
    site = tmp_path / "release"
    stage_public(db.path, site)
    index = json.loads((site / "index.json").read_text())
    wanted = [row for row in index["entries"] if row["id"] == FIRST]
    assert _versions(site, FIRST)["entries"] == sorted(
        wanted, key=lambda row: row["version"]
    )


def test_a_withdrawn_version_leaves_the_document(db, tmp_path):
    db.add_entry(FIRST, 2)
    db.write_json("takedowns.json", {
        "schema_version": 1,
        "takedowns": [{
            "id": FIRST, "version": 1,
            "taken_down_at": "2026-08-06T12:00:00Z",
            "authorized_by_login": "avigad", "authorization_issue": 101, "reason": "a private reason",
        }],
    })
    site = tmp_path / "release"
    stage_public(db.path, site)
    assert [row["version"] for row in _versions(site, FIRST)["entries"]] == [2]


def test_an_incremental_new_version_keeps_a_prior_withdrawal_out(
    repo, tmp_path, monkeypatch
):
    """The per-result served projection replaces a global W-target scan."""
    repo.write_json(
        "takedowns.json",
        {
            "schema_version": 1,
            "takedowns": [{
                "id": FIRST,
                "version": 1,
                "taken_down_at": "2026-08-06T12:00:00Z",
                "authorized_by_login": "avigad",
                "authorization_issue": 102,
                "reason": "a private reason",
            }],
        },
    )
    repo.commit("withdraw version one")
    served = tmp_path / "served"
    stage_public(repo.path, served, full=True)
    prior_delta = release_delta.parse((served / "release-delta.json").read_bytes())
    repo.add_entry(FIRST, 2)
    repo.commit("accept version two without changing takedowns")

    def no_global_manifest(*_args, **_kwargs):
        raise AssertionError("incremental staging enumerated takedowns.json")

    monkeypatch.setattr(staging, "load_takedowns", no_global_manifest)
    site = tmp_path / "incremental"
    stage_public(
        repo.path,
        site,
        previous=release_delta.base_of(prior_delta),
        prior=served,
        require_incremental=True,
    )
    delta = release_delta.parse((site / "release-delta.json").read_bytes())
    assert delta["parent"] == release_delta.release_id(prior_delta)
    assert [row["version"] for row in _versions(site, FIRST)["entries"]] == [2]
    assert not any(
        row["path"].startswith("tombstones/") for row in delta["stable"]
    )

    shutil.copytree(site, served, dirs_exist_ok=True)
    monkeypatch.undo()
    rebuilt = tmp_path / "rebuilt"
    stage_public(repo.path, rebuilt, full=True)
    assert surfaces(served) == surfaces(rebuilt)


@pytest.mark.parametrize(
    ("malformation", "message"),
    [
        ("symlink", "symbolic versions"),
        ("missing", "missing versions"),
        ("json", "invalid versions"),
        ("schema", "invalid versions"),
        ("disagreement", "projection-disagreeing versions"),
    ],
)
def test_an_unusable_prior_version_projection_requests_a_full_rebuild(
    repo, tmp_path, malformation, message
):
    repo.commit("the served base")
    served = tmp_path / "served"
    stage_public(repo.path, served, full=True)
    prior_delta = release_delta.parse((served / "release-delta.json").read_bytes())
    repo.add_entry(FIRST, 2)
    repo.commit("accept a second version")
    version_path = served / "versions" / f"{FIRST}.json"
    if malformation == "symlink":
        version_path.unlink()
        version_path.symlink_to("elsewhere.json")
    elif malformation == "missing":
        version_path.unlink()
    elif malformation == "json":
        version_path.write_text("{\n", encoding="utf-8")
    elif malformation == "schema":
        document = json.loads(version_path.read_text(encoding="utf-8"))
        document["schema_version"] = 1
        version_path.write_text(json.dumps(document) + "\n", encoding="utf-8")
    else:
        document = json.loads(version_path.read_text(encoding="utf-8"))
        document["entries"][0]["title"] = "drifted title"
        version_path.write_text(json.dumps(document) + "\n", encoding="utf-8")

    output = tmp_path / "refused"
    with pytest.raises(FullCheckoutRequired, match=message):
        stage_public(
            repo.path,
            output,
            previous=release_delta.base_of(prior_delta),
            prior=served,
            require_incremental=True,
        )
    assert not output.exists()


def test_an_identifier_with_every_version_withdrawn_keeps_an_empty_document(db, tmp_path):
    """The same answer its tombstones give, and the only one two kinds of
    release can agree on.

    A release that writes the documents it touched has this one in hand and
    writes it empty; a release that writes all of them would have to know that
    it once existed in order to stop writing it, and it does not. A 404 was the
    tidier answer while there was only ever one kind of release.
    """
    db.write_json("takedowns.json", {
        "schema_version": 1,
        "takedowns": [{
            "id": FIRST, "version": 1,
            "taken_down_at": "2026-08-06T12:00:00Z",
            "authorized_by_login": "avigad", "authorization_issue": 103, "reason": "a private reason",
        }],
    })
    site = tmp_path / "release"
    stage_public(db.path, site)
    assert _versions(site, FIRST)["entries"] == []
    assert (site / "tombstones" / f"{FIRST}-v1.json").is_file()


def test_an_absurdly_versioned_identifier_is_refused_not_trimmed(db, tmp_path, monkeypatch):
    """Truncating would drop history from the one surface that exists to show
    it. Nothing should reach this; if something does, that is what to look at."""
    monkeypatch.setattr(registration_projection, "MAX_VERSIONS_PER_RESULT", 1)
    db.add_entry(FIRST, 2)
    with pytest.raises(ValueError, match="more than 1 versions"):
        stage_public(db.path, tmp_path / "release")


def test_a_new_result_does_not_rewrite_every_other_version_index(repo, tmp_path):
    """The growth class, and the whole reason these sit at stable keys."""
    written = {}
    served = tmp_path / "served"
    served.mkdir()
    for size in (2, 20):
        for serial in range(1, size):
            repo.add_entry(f"PALOMAR-2026-07-28-{serial:06d}", 1)
        repo.commit(f"a registry of {size}")
        base = tmp_path / f"base{size}"
        stage_public(repo.path, base)
        shutil.rmtree(served)
        shutil.copytree(base, served)
        previous = release_delta.parse((base / "release-delta.json").read_bytes())
        newcomer = repo.next_identifier()
        repo.add_entry(newcomer, 1)
        repo.commit("one more")
        site = tmp_path / f"next{size}"
        stage_public(
            repo.path,
            site,
            previous=release_delta.base_of(previous),
            prior=served,
        )
        shutil.copytree(site, served, dirs_exist_ok=True)
        written[size] = len(list((site / "versions").glob("*.json")))
        repo.remove(f"entries/{newcomer}-v1.json")
        repo.reindex()
    assert written[2] == written[20], written
    assert written[2] == 1, "it wrote something other than the one that changed"


def test_a_version_index_is_rebuilt_rather_than_patched(repo, tmp_path):
    """An unusable served index requests the safe full-rebuild fallback.

    The incremental path normally combines the bounded result projection with
    the exact served per-result active subset. With no served document it cannot
    do so, and must not invent the subset from historical rows.
    """
    repo.commit("a starting point")
    stage_public(repo.path, tmp_path / "first")
    first = release_delta.parse((tmp_path / "first" / "release-delta.json").read_bytes())
    repo.add_entry(FIRST, 2)
    repo.commit("a second version")

    site = tmp_path / "second"
    # Nothing at all is being served, so nothing could have been patched.
    (tmp_path / "empty").mkdir()
    stage_public(
        repo.path,
        site,
        previous=release_delta.base_of(first),
        prior=tmp_path / "empty",
    )

    assert [row["version"] for row in _versions(site, FIRST)["entries"]] == [1, 2]
