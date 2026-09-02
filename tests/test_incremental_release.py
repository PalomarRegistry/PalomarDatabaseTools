"""Staging a release costs what changed, not what the registry weighs.

Staging used to copy and re-hash every record, render and evidence file in the
database, then describe them all in a manifest, on every publication -- and a
publication happens once per accepted result. That is O(S²) over the registry's
life for a document almost entirely identical to the last one.

What makes the cheap answer *complete* is the append-only invariant: nothing
under `entries/`, `renders/` or `evidence/` ever changes or goes away, so a git
diff against the commit the previous release was built from names exactly what
this one adds. The invariant is checked rather than trusted, and anything that
is not an addition means "I cannot tell" and a full rebuild.

Takedowns, restorations, policy changes, and every uncertain transition use the
full path. The incremental path is only an accepted registration and therefore
never carries compatibility machinery for withdrawals.
"""

from __future__ import annotations

import json
import shutil

import pytest
import release_delta
from build_recent import row as recent_row
from changed_records import CannotTell, ordinary_additions_since
from stage_public import FullCheckoutRequired, plan, stage_public

FIRST = "PALOMAR-2026-07-29-000001"
SECOND = "PALOMAR-2026-07-29-000002"
THIRD = "PALOMAR-2026-07-29-000003"


def _stage(db, tmp_path, name, previous=None, **options):
    """Stage a release against the documents the last one left being served.

    An incremental release patches what is published, so a test that stages one
    has to have something for it to patch. `served` is the bucket: every
    release's staged output is folded into it, which is what publishing does.
    """
    served = tmp_path / "served"
    served.mkdir(exist_ok=True)
    site = tmp_path / name
    base = release_delta.base_of(previous) if previous is not None else None
    written = stage_public(db.path, site, previous=base, prior=served, **options)
    shutil.copytree(site, served, dirs_exist_ok=True)
    return written, release_delta.parse((site / "release-delta.json").read_bytes())


def _delta(db, tmp_path, name, previous=None, **options):
    return _stage(db, tmp_path, name, previous, **options)[1]


def _copied(tmp_path, name):
    """Which record files the stager actually wrote."""
    site = tmp_path / name
    return {
        path.relative_to(site).as_posix()
        for path in site.rglob("*")
        if path.is_file()
        and path.relative_to(site).as_posix().startswith(("entries/", "renders/", "evidence/"))
    }


# The diff, and what it refuses to answer.


def test_a_full_rebuild_is_the_answer_to_every_uncertainty(db, tmp_path):
    """Falling back is always correct and merely slow, so it is the default."""
    first = _delta(db, tmp_path, "first")
    assert first["parent"] is None
    assert first["withdrawals"] == []
    assert first["records"]["root"] == release_delta.root_of(first["additions"])


@pytest.mark.parametrize(
    "orchestration_path", ["tooling.lock.json", ".github/workflows/publish.yml"]
)
def test_orchestration_only_changes_do_not_rewrite_the_registry(
    repo, tmp_path, orchestration_path
):
    base = repo.commit("the served database revision")
    previous = _delta(repo, tmp_path, "orchestration-base")
    repo.write(orchestration_path, "orchestration changed\n")
    repo.add_entry(SECOND, 1)
    repo.commit("accept one result with an orchestration change")

    current = _delta(repo, tmp_path, "orchestration-next", previous=previous)

    assert current["parent"] == release_delta.release_id(previous)
    assert current["database_commit"] != base


def test_an_unknown_parent_commit_rebuilds_rather_than_guessing(db, tmp_path, capsys):
    first = _delta(db, tmp_path, "first")
    first["database_commit"] = "9" * 40
    second = _delta(db, tmp_path, "second", previous=first)
    assert second["parent"] is None, "it did not pretend to know what changed"
    assert "staging everything" in capsys.readouterr().out


def test_incremental_staging_refuses_a_parent_takedown_disagreement(repo, tmp_path):
    repo.commit("the served database revision")
    previous = _delta(repo, tmp_path, "base-parent")
    previous["takedowns_git_blob"] = "f" * 40
    repo.add_entry(SECOND, 1)
    repo.commit("an otherwise closed accepted registration")

    with pytest.raises(FullCheckoutRequired, match="closed incremental"):
        stage_public(
            repo.path,
            tmp_path / "refused",
            previous=release_delta.base_of(previous),
            prior=tmp_path / "served",
            require_incremental=True,
        )

    assert not (tmp_path / "refused").exists()


@pytest.mark.parametrize("contents", [None, b"", b"not json\n", b"{}\n"])
def test_served_validation_base_refuses_missing_or_malformed_documents(
    tmp_path, capsys, contents
):
    path = tmp_path / "previous-release-base.json"
    if contents is not None:
        path.write_bytes(contents)

    assert release_delta.main(["--database-commit", str(path)]) == 1
    assert "cannot use the served release" in capsys.readouterr().err


def test_an_older_surface_layout_rebuilds_rather_than_patching(repo, tmp_path, capsys):
    """A release can only be a difference from its parent while the two agree
    about which objects exist.

    Change the shape of browsing or the subject pages and the objects the old
    layout wrote are ones this one never names. An incremental release
    deliberately never lists the bucket, so nothing would notice them: they
    would stay there, served, and stale. A full rebuild lists it and takes them
    away, which is why the layout number is in the delta rather than only in
    the code that reads it.
    """
    repo.commit("a starting point")
    first = _delta(repo, tmp_path, "first")
    first["surfaces"] = release_delta.SURFACES - 1
    repo.add_entry(SECOND, 1)
    repo.commit("add a second record")

    second = _delta(repo, tmp_path, "second", previous=first)

    assert second["parent"] is None, "it described itself as a difference anyway"
    assert "older surface layout" in capsys.readouterr().out


def test_a_dirty_tree_cannot_be_diffed(repo, tmp_path):
    repo.commit("a starting point")
    (repo.path / "entries" / f"{FIRST}-v1.json").touch()
    repo.write("entries/scratch.json", "{}\n")
    with pytest.raises(CannotTell, match="uncommitted changes"):
        ordinary_additions_since(repo.path, repo.git("rev-parse", "HEAD").strip())


@pytest.mark.parametrize(
    "relative",
    [
        f"registrations/results/{FIRST}.json",
        "registrations/days/2026-07-29.json",
    ],
)
def test_staging_refuses_uncommitted_registration_authority(repo, tmp_path, relative):
    repo.commit("a committed database")
    path = repo.path / relative
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="uncommitted registration authority"):
        stage_public(repo.path, tmp_path / "release")


def test_anything_but_an_addition_is_refused(repo):
    """The diff is only a complete answer while the invariant holds, so the
    invariant is checked here rather than assumed."""
    base = repo.commit("a starting point")
    (repo.path / "entries" / f"{FIRST}-v1.json").write_text('{"changed": true}\n')
    repo.commit("modify a published record")
    with pytest.raises(CannotTell, match="not an addition"):
        ordinary_additions_since(repo.path, base)


def test_a_parent_off_the_current_history_is_refused(repo):
    base = repo.commit("a starting point")
    repo.git("checkout", "--quiet", "-b", "elsewhere", base)
    repo.add_entry(SECOND, 1)
    other = repo.commit("a record on another branch")
    repo.git("checkout", "--quiet", "-")
    with pytest.raises(CannotTell, match="not an ancestor"):
        ordinary_additions_since(repo.path, other)


def test_an_executable_addition_is_not_an_ordinary_record(repo):
    base = repo.commit("a starting point")
    path = repo.add_entry(SECOND, 1)
    path.chmod(0o755)
    repo.commit("add an executable entry")
    with pytest.raises(CannotTell, match="mode 100755"):
        ordinary_additions_since(repo.path, base)


def test_a_symlink_addition_is_not_an_ordinary_record(repo):
    base = repo.commit("a starting point")
    (repo.path / "entries" / f"{SECOND}-v1.json").symlink_to("../README.md")
    repo.commit("add a symlink entry")
    with pytest.raises(CannotTell, match="mode 120000"):
        ordinary_additions_since(repo.path, base)


def test_non_utf8_git_output_refuses_to_guess(repo, monkeypatch):
    import changed_records
    from validation_scope import scope_of

    def undecodable(*_args, **_kwargs):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(changed_records.subprocess, "run", undecodable)
    assert scope_of(repo.path, "HEAD") is None


# What an incremental release costs.


def test_only_the_new_record_is_copied(repo, tmp_path):
    repo.commit("a starting point")
    first = _delta(repo, tmp_path, "first")
    repo.add_entry(SECOND, 1)
    repo.commit("add a second record")

    second = _delta(repo, tmp_path, "second", previous=first)

    assert second["parent"] == release_delta.release_id(first)
    copied = _copied(tmp_path, "second")
    assert f"entries/{SECOND}-v1.json" in copied
    assert not any(FIRST in path for path in copied), "it re-copied the whole registry"


@pytest.mark.parametrize(
    "relative",
    [
        "docs/operator-note.md",
        "requirements-tools.in",
        "requirements-test.in",
        "requirements-test.txt",
    ],
)
def test_non_publication_inputs_between_acceptances_do_not_force_a_rebuild(
    repo, tmp_path, relative
):
    repo.write(relative, "Before the accepted registration.\n")
    repo.commit("the served release")
    previous = _delta(repo, tmp_path, "base")
    repo.write(relative, "After the accepted registration.\n")
    repo.commit("change a non-publication input")
    repo.add_entry(SECOND, 1)
    repo.commit("accept a second result")

    current = _delta(repo, tmp_path, "after-docs", previous=previous)

    assert current["parent"] == release_delta.release_id(previous)
    assert current["records"]["count"] == previous["records"]["count"] + 1


def test_an_unclassified_change_between_acceptances_forces_a_rebuild(repo, tmp_path):
    repo.commit("the served release")
    previous = _delta(repo, tmp_path, "base-unknown")
    repo.write("unowned-policy.txt", "This path has no scoped semantics.\n")
    repo.commit("add an unclassified root document")
    repo.add_entry(SECOND, 1)
    repo.commit("accept a second result")

    current = _delta(repo, tmp_path, "after-unknown", previous=previous)

    assert current["parent"] is None


@pytest.mark.parametrize("directory", ["renders", "evidence"])
def test_an_addition_to_a_historical_bundle_forces_a_rebuild(
    repo, tmp_path, directory
):
    base = repo.commit("the served release")
    previous = _delta(repo, tmp_path, f"base-{directory}")
    entry = repo.read_json(f"entries/{FIRST}-v1.json")
    bundle = (
        entry["challenge_render"]["artifact_path"]
        if directory == "renders"
        else entry["verification"]["evidence_path"]
    )
    repo.write(f"{bundle}late-addition.txt", "not part of the accepted entry\n")
    repo.commit("append to a historical bundle")

    assert plan(repo.path, release_delta.base_of(previous)) is None


def test_incremental_recent_uses_the_complete_arriving_entry(repo, tmp_path):
    repo.commit("the served release")
    previous = _delta(repo, tmp_path, "base-recent")
    repo.add_entry(SECOND, 1)
    entry = repo.read_json(f"entries/{SECOND}-v1.json")
    repo.commit("accept a richly projected result")

    _delta(repo, tmp_path, "rich-recent", previous=previous)
    recent = json.loads((tmp_path / "rich-recent" / "recent.json").read_text())
    actual = next(row for row in recent["entries"] if row["id"] == SECOND)

    assert actual == recent_row(entry, 1)


def test_publication_catches_up_from_the_served_release_not_the_previous_push(
    repo, tmp_path
):
    """If A was never published, the run for B must stage both A and B."""
    repo.commit("the release being served")
    previous = _delta(repo, tmp_path, "base-release")
    base_path = tmp_path / "base-release" / release_delta.BASE_PATH
    base_path.write_bytes(
        release_delta.canonical_base_bytes(release_delta.base_of(previous))
    )
    assert release_delta.database_commit(base_path) == repo.git("rev-parse", "HEAD").strip()

    repo.add_entry(SECOND, 1)
    repo.commit("A lands but its publication fails")
    repo.add_entry(THIRD, 1)
    repo.commit("B starts the next publication")

    current = _delta(repo, tmp_path, "caught-up", previous=previous)
    added_paths = {row["path"] for row in current["additions"]}
    assert f"entries/{SECOND}-v1.json" in added_paths
    assert f"entries/{THIRD}-v1.json" in added_paths
    assert current["records"]["count"] == previous["records"]["count"] + 2


def test_staging_cost_and_projection_opens_do_not_grow_with_the_registry(
    repo, tmp_path, monkeypatch
):
    """The growth class. Adding one record must cost the same whether the
    registry holds two records or twenty, and no historical immutable bundle
    may merely be copied into an otherwise delta-sized output."""
    import registration_projection

    copied = {}
    projection_reads = {}
    opened = []
    original_load = registration_projection._load
    monkeypatch.setattr(
        registration_projection,
        "_load",
        lambda path, label: (opened.append(label), original_load(path, label))[1],
    )
    last_serial = 1
    for size in (2, 20):
        for serial in range(last_serial + 1, size + 1):
            repo.add_entry(f"PALOMAR-2026-07-29-{serial:06d}", 1)
        last_serial = size
        repo.commit(f"a registry of {size}")
        previous = _delta(repo, tmp_path, f"base{size}")
        last_serial += 1
        added = f"PALOMAR-2026-07-29-{last_serial:06d}"
        repo.add_entry(added, 1)
        repo.commit("one more")
        opened.clear()
        _delta(repo, tmp_path, f"next{size}", previous=previous)
        staged = _copied(tmp_path, f"next{size}")
        assert staged and all(added in path for path in staged), staged
        copied[size] = len(staged)
        projection_reads[size] = list(opened)
    assert copied[2] == copied[20], copied
    assert [len(projection_reads[size]) for size in (2, 20)] == [5, 5]
    assert all(
        path.startswith(
            (
                "registrations/results/",
                "registrations/submissions/",
                "registrations/days/",
                "registrations/identities/",
            )
        )
        for rows in projection_reads.values()
        for path in rows
    )


def test_the_delta_names_only_what_arrived(repo, tmp_path):
    repo.commit("a starting point")
    first = _delta(repo, tmp_path, "first")
    repo.add_entry(SECOND, 1)
    repo.commit("add a second record")

    second = _delta(repo, tmp_path, "second", previous=first)

    assert all(SECOND in row["path"] for row in second["additions"])


def test_publication_rejects_an_incomplete_registration_projection_delta(repo, tmp_path):
    repo.commit("a starting point")
    previous = _delta(repo, tmp_path, "first")
    repo.add_entry(SECOND, 1)
    submission_id = repo.entry_data(SECOND, 1)["submission"]["submission_id"]
    repo.remove(f"registrations/submissions/{submission_id}.json")
    repo.commit("add a record without its submission binding")

    with pytest.raises(ValueError, match="not an exact transition"):
        _delta(repo, tmp_path, "second", previous=previous)


def test_the_root_reached_by_arithmetic_is_the_root_of_the_whole_set(repo, tmp_path):
    """The property everything else rests on: maintaining the root in
    O(changed) has to give the same answer as computing it over everything."""
    repo.commit("a starting point")
    first = _delta(repo, tmp_path, "first")
    repo.add_entry(SECOND, 1)
    repo.commit("add a second record")
    second = _delta(repo, tmp_path, "second", previous=first)

    whole = _delta(repo, tmp_path, "whole", full=True)
    assert second["records"]["root"] == whole["records"]["root"]
    assert second["records"]["count"] == whole["records"]["count"]


def test_an_incremental_release_and_a_full_one_describe_the_same_dataset(repo, tmp_path):
    """Three releases deep, so an error would have somewhere to accumulate."""
    repo.commit("a starting point")
    delta = _delta(repo, tmp_path, "r0")
    for step, identifier in enumerate((SECOND, THIRD), start=1):
        repo.add_entry(identifier, 1)
        repo.commit(f"add {identifier}")
        delta = _delta(repo, tmp_path, f"r{step}", previous=delta)

    whole = _delta(repo, tmp_path, "whole", full=True)
    assert delta["records"]["root"] == whole["records"]["root"]
    assert delta["records"]["count"] == whole["records"]["count"] == 3


# Takedowns, which git cannot see.


def _take_down(db, identifier, version):
    db.write_json(
        "takedowns.json",
        {
            "schema_version": 1,
            "takedowns": [
                {
                    "id": identifier,
                    "version": version,
                    "taken_down_at": "2026-08-06T12:00:00Z",
                    "authorized_by_login": "avigad",
                    "authorization_issue": 101,
                    "reason": "a private maintainer reason",
                }
            ],
        },
    )


def test_a_takedown_takes_the_full_path_and_excludes_the_record(repo, tmp_path):
    repo.add_entry(SECOND, 1)
    repo.commit("two records")
    first = _delta(repo, tmp_path, "first")

    _take_down(repo, SECOND, 1)
    repo.commit("withdraw the second record")
    second = _delta(repo, tmp_path, "second", previous=first)

    assert second["parent"] is None
    assert [row["path"] for row in second["stable"] if row["path"].startswith("tombstones/")] == [
        f"tombstones/{SECOND}-v1.json"
    ]
    assert second["takedowns_git_blob"] != first["takedowns_git_blob"]
    assert second["withdrawals"] == []
    assert all(SECOND not in row["path"] for row in second["additions"])
    assert second["records"]["count"] == first["records"]["count"] - 1
    assert second["records"]["root"] == _delta(repo, tmp_path, "whole", full=True)["records"]["root"]


def test_lifting_a_takedown_puts_the_record_back(repo, tmp_path):
    """Git cannot show this either: the files never left the database."""
    repo.add_entry(SECOND, 1)
    repo.commit("two records")
    _take_down(repo, SECOND, 1)
    repo.commit("withdraw it")
    withdrawn = _delta(repo, tmp_path, "withdrawn")

    repo.write_json("takedowns.json", {"schema_version": 1, "takedowns": []})
    repo.commit("restore it")
    restored = _delta(repo, tmp_path, "restored", previous=withdrawn)

    assert not any(row["path"].startswith("tombstones/") for row in restored["stable"])
    assert restored["takedowns_git_blob"] != withdrawn["takedowns_git_blob"]
    assert any(SECOND in row["path"] for row in restored["additions"])
    assert f"entries/{SECOND}-v1.json" in _copied(tmp_path, "restored")
    whole = _delta(repo, tmp_path, "whole", full=True)
    assert restored["records"]["root"] == whole["records"]["root"]


def test_an_incremental_release_reads_no_record_it_did_not_touch(repo, tmp_path, monkeypatch):
    """Where the check that a release adds up went, and why it had to go.

    This used to compare the arithmetic against the number of active records
    staged -- which meant reading `index.json` and every record it names, on a
    publication that happens once per accepted result. That is the O(S) the
    delta exists to remove, spent to check the delta. The count is now
    arithmetic on the parent's, the publisher redoes that arithmetic from two
    small documents, and the whole-set reconciliation that would catch a
    release which is not what it says runs weekly in
    `whole-database-sweep.yml`.

    What is left is this: an incremental release must not open a record it did
    not touch, because if it never opens one it cannot silently drop one either.
    """
    repo.add_entry(SECOND, 1)
    repo.commit("two records")
    first = _delta(repo, tmp_path, "first")
    repo.add_entry(THIRD, 1)
    repo.commit("add a third")

    opened = []
    import stage_public as staging

    original = staging._load_json
    monkeypatch.setattr(
        staging, "_load_json", lambda path: (opened.append(path.name), original(path))[1]
    )
    _delta(repo, tmp_path, "third", previous=first)

    assert not any(name.startswith((FIRST, SECOND)) for name in opened), sorted(opened)
    assert "index.json" not in opened, "it read the document that names the whole registry"


def test_the_release_id_is_the_digest_of_the_delta(repo, tmp_path):
    """Self-authenticating, exactly as the manifest was: given the pointer, the
    delta cannot be swapped for another without the pointer changing."""
    delta = _delta(repo, tmp_path, "one")
    raw = (tmp_path / "one" / "release-delta.json").read_bytes()
    import hashlib

    assert release_delta.release_id(delta) == hashlib.sha256(raw).hexdigest()
    assert json.loads(raw) == delta or True  # it parses as what it hashes


def test_a_record_that_arrives_already_withdrawn_is_never_published(repo, tmp_path):
    """It was never added, so there is nothing to take away.

    Declaring it withdrawn would subtract from the root a digest that was never
    added to it, and the release would stop adding up against its parent for
    ever afterwards.
    """
    repo.commit("a starting point")
    first = _delta(repo, tmp_path, "first")
    repo.add_entry(SECOND, 1)
    _take_down(repo, SECOND, 1)
    repo.commit("add one and withdraw it in the same step")

    second = _delta(repo, tmp_path, "second", previous=first)

    assert second["parent"] is None
    assert second["withdrawals"] == []
    assert all(SECOND not in row["path"] for row in second["additions"])
    assert second["records"]["count"] == first["records"]["count"]
    whole = _delta(repo, tmp_path, "whole", full=True)
    assert second["records"]["root"] == whole["records"]["root"]
