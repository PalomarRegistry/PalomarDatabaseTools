"""A patched page has to be the page a rebuild would have written.

This is the one failure this design can have and the only one nothing else
would catch. Every other property here is checked against a single release: the
pages are well formed, the counts add up, the feed matches its page. None of
them notice a patch that quietly drifts from what building the same dataset
from the records would produce, because both answers are well formed and the
disagreement only appears months later as a page nobody can explain.

So the comparison is byte for byte, over the whole set of derived pages, after
a sequence of releases rather than after one: an arithmetic error in a count
that is maintained by addition has to have somewhere to accumulate before it
shows.
"""

from __future__ import annotations

import json

import pytest
from conftest import surfaces
from stage_public import FullCheckoutRequired, stage_public

FIRST = "PALOMAR-2026-07-29-000001"


def _classified(database, identifier, version=1, arxiv=("math.CO",), msc=("05C10",), **overrides):
    entry = database.entry_data(identifier, version)
    entry["classification"] = {"arxiv": list(arxiv), "msc2020": list(msc)}
    entry.update(overrides)
    database.install_entry(entry)
    return entry


def _identified(database, identifier, repository, comparator_path):
    entry = database.entry_data(identifier, 1, registered_at=_registered(int(identifier[-2:])))
    entry["source"]["repository"] = repository
    entry["source"]["repository_url"] = f"https://github.com/{repository}"
    entry["source"]["tree_url"] = (
        f"https://github.com/{repository}/tree/{entry['source']['commit']}"
    )
    entry["formalization"]["comparator_config_path"] = comparator_path
    database.install_entry(entry)
    return entry


def _registered(day):
    """A distinct registration instant, which is what every surface orders on.

    Distinct per record on purpose. Rows that all carried one instant would come
    out in identifier order whatever the ordering rule was, so a patched page
    would agree with a rebuilt one even if the two ordered by different things,
    which is the disagreement these tests exist to find.
    """
    return f"2026-08-{day:02d}T00:00:00Z"


def _take_down(database, identifier, version):
    database.write_json("takedowns.json", {
        "schema_version": 1,
        "takedowns": [{
            "id": identifier, "version": version,
            "taken_down_at": "2026-08-06T12:00:00Z",
            "authorized_by_login": "avigad", "authorization_issue": 101, "reason": "a private reason",
        }],
    })


def _agree(served):
    patched = surfaces(served.path)
    rebuilt = surfaces(served.rebuild())
    assert sorted(patched) == sorted(rebuilt), "the two releases publish different documents"
    for relative, bytes_ in sorted(rebuilt.items()):
        assert patched[relative] == bytes_, f"{relative} differs from what a rebuild writes"


def test_a_sequence_of_incremental_releases_matches_a_rebuild(served):
    """The plain case, four releases deep so that an error can accumulate."""
    served.database.commit("a starting point")
    served.publish()
    for serial in (2, 3, 4):
        _classified(
            served.database,
            f"PALOMAR-2026-07-29-{serial:06d}",
            registered_at=_registered(serial),
        )
        served.database.commit(f"add {serial}")
        served.publish()

    _agree(served)


def test_repository_and_identity_lookups_patch_across_distinct_groups(served):
    served.database.commit("a starting point")
    served.publish()
    _identified(
        served.database,
        "PALOMAR-2026-07-29-000002",
        "another-owner/another-project",
        "comparator.json",
    )
    served.database.commit("add another repository")
    served.publish()
    _identified(
        served.database,
        "PALOMAR-2026-07-29-000003",
        "kim-em/erdos-unit-distance-comparator",
        "alternate/comparator.json",
    )
    served.database.commit("add another identity")
    served.publish()

    _agree(served)


def test_a_new_version_of_an_existing_result_matches_a_rebuild(served):
    """The row moves to the top of what is new while staying on the browse page
    its first version's identifier put it on, which is the case a layout keyed
    on anything the later version carries gets wrong."""
    _classified(served.database, FIRST, 1, registered_at=_registered(1))
    served.database.commit("a starting point")
    served.publish()
    _classified(served.database, FIRST, 2, registered_at=_registered(9))
    served.database.commit("a second version")
    served.publish()

    _agree(served)


def test_a_version_that_changes_its_classification_matches_a_rebuild(served):
    """It leaves one code's pages and joins another's, and neither is found by
    looking anything up."""
    _classified(served.database, FIRST, 1, msc=("05C10",), arxiv=("math.CO",))
    served.database.commit("a starting point")
    served.publish()
    _classified(served.database, FIRST, 2, msc=("68R10",), arxiv=("cs.DM",),
                registered_at=_registered(9))
    served.database.commit("reclassify it")
    served.publish()

    _agree(served)
    assert json.loads((served.path / "subjects/msc/05C10.json").read_text())["entries"] == []


def test_a_takedown_of_a_result_on_a_sealed_day_matches_a_rebuild(served):
    """A sealed day is not an immutable one. A withdrawal rewrites one page of
    a day that is long past, which is bounded and fine; what it must not do is
    leave that page saying something a rebuild would not."""
    _classified(served.database, "PALOMAR-2026-07-29-000002", 1, registered_at=_registered(2))
    served.database.commit("a starting point")
    served.publish()
    for serial in range(1, 4):
        _classified(served.database, f"PALOMAR-2026-08-20-{serial:06d}", 1,
                    registered_at=_registered(20))
    served.database.commit("a later day")
    served.publish()

    _take_down(served.database, "PALOMAR-2026-07-29-000002", 1)
    served.database.commit("withdraw one from the sealed day")
    served.publish()

    _agree(served)


def test_lifting_a_takedown_matches_a_rebuild(served):
    """Git sees nothing either way, so both directions come out of the
    difference between two takedown sets."""
    _classified(served.database, "PALOMAR-2026-07-29-000002", 1, registered_at=_registered(2))
    served.database.commit("a starting point")
    served.publish()
    _take_down(served.database, "PALOMAR-2026-07-29-000002", 1)
    served.database.commit("withdraw it")
    served.publish()
    served.database.write_json("takedowns.json", {"schema_version": 1, "takedowns": []})
    served.database.commit("restore it")
    served.publish()

    _agree(served)


# The adversarial distributions.


def test_a_whole_registry_registered_on_one_day_matches_a_rebuild(served):
    for serial in range(2, 12):
        _classified(served.database, f"PALOMAR-2026-07-29-{serial:06d}", 1,
                    registered_at=_registered(2))
    served.database.commit("a starting point")
    served.publish()
    _classified(served.database, "PALOMAR-2026-07-29-000012", 1, registered_at=_registered(2))
    served.database.commit("one more on the same day")
    served.publish()

    _agree(served)


def test_a_day_with_more_results_than_one_page_matches_a_rebuild(served, monkeypatch):
    """The new result opens a second page for its day, which is the one moment
    the day's row in the year document has to move."""
    monkeypatch.setattr("day_pages.PAGE_SERIALS", 4)
    for serial in range(2, 5):
        _classified(served.database, f"PALOMAR-2026-07-29-{serial:06d}", 1,
                    registered_at=_registered(2))
    served.database.commit("a first page")
    served.publish()
    for serial in range(5, 8):
        _classified(served.database, f"PALOMAR-2026-07-29-{serial:06d}", 1,
                    registered_at=_registered(2))
    served.database.commit("over the edge of it")
    served.publish()

    assert (served.path / "browse/2026-07-29/2.json").is_file(), "no second page opened"
    _agree(served)


def test_every_result_under_one_code_matches_a_rebuild(served):
    for serial in range(2, 12):
        _classified(served.database, f"PALOMAR-2026-07-29-{serial:06d}", 1,
                    msc=("05C10",), registered_at=_registered(2))
    served.database.commit("a starting point")
    served.publish()
    _classified(served.database, "PALOMAR-2026-07-29-000012", 1, msc=("05C10",),
                registered_at=_registered(3))
    served.database.commit("one more under the same code")
    served.publish()

    _agree(served)


def test_a_quiet_day_matches_a_rebuild(served):
    """A day with nothing registered on it has no row and no page, so nothing
    has to be written to say so."""
    served.database.commit("a starting point")
    served.publish()
    _classified(served.database, "PALOMAR-2026-08-11-000001", 1, registered_at=_registered(11))
    served.database.commit("a much later day")
    served.publish()

    assert [row["day"] for row in json.loads(
        (served.path / "browse/2026.json").read_text()
    )["days"]] == ["2026-07-29", "2026-08-11"]
    _agree(served)


def test_a_result_registered_in_another_year_matches_a_rebuild(served):
    served.database.commit("a starting point")
    served.publish()
    _classified(served.database, "PALOMAR-2027-03-04-000001", 1, registered_at=_registered(11))
    served.database.commit("the next year")
    served.publish()

    assert [row["year"] for row in json.loads(
        (served.path / "browse/index.json").read_text()
    )["years"]] == ["2026", "2027"]
    _agree(served)


# The plan, which is worked out where there are no credentials to work it out
# with, and is all the credentialed step is given.


def test_the_plan_names_every_document_the_patch_reads(served):
    """Nothing else is fetched, so a plan that under-named one would leave the
    stager treating a page that exists as one that never did, and writing it
    back with everything else on it gone."""
    _classified(served.database, "PALOMAR-2026-07-29-000002", 1, registered_at=_registered(2))
    served.database.commit("a starting point")
    served.publish(by_plan=True)
    _classified(served.database, "PALOMAR-2026-07-29-000003", 1, registered_at=_registered(3))
    served.database.commit("add one")
    served.publish(by_plan=True)
    _classified(served.database, "PALOMAR-2026-07-29-000002", 2, msc=("68R10",),
                registered_at=_registered(9))
    served.database.commit("reclassify another")
    served.publish(by_plan=True)

    _agree(served)


def test_the_plan_is_worked_out_from_the_database_and_the_parent_alone(served):
    """No bucket, no staging directory, no records but the ones it names. That
    is what lets the step that decides what a release does run without
    credentials and the step that has them decide nothing."""
    import stage_public

    served.database.commit("a starting point")
    served.publish()
    _classified(served.database, "PALOMAR-2026-07-29-000002", 1, registered_at=_registered(2))
    served.database.commit("add one")

    ready = stage_public.plan(
        served.database.path.resolve(), __import__("release_delta").base_of(served.delta)
    )

    assert ready is not None
    assert [item.id for item in ready.touched] == ["PALOMAR-2026-07-29-000002"]
    assert "recent.json" in ready.prior_paths()
    assert "browse/2026-07-29/1.json" in ready.prior_paths()
    assert "subjects/msc/05C10.json" in ready.prior_paths()


# When patching cannot answer, and says so.


def test_a_capped_page_that_loses_a_row_rebuilds_rather_than_shortening(served, monkeypatch, capsys):
    """The one thing a patch cannot do. The newest fifty of a set is not a
    function of the parent's newest fifty and the change: take a row out and
    what belongs in its place is the next one down, which is not here.

    A page that was already short held everything there was, so this only ever
    happens to a full one -- and the answer to it is a rebuild, which is always
    correct and merely slow.
    """
    monkeypatch.setattr("build_subjects.SUBJECT_PAGE_ITEMS", 2)
    monkeypatch.setattr("patch_surfaces.SUBJECT_PAGE_ITEMS", 2)
    for serial in range(2, 6):
        _classified(served.database, f"PALOMAR-2026-07-29-{serial:06d}", 1,
                    msc=("05C10",), registered_at=_registered(serial))
    served.database.commit("a starting point")
    served.publish()

    _classified(served.database, "PALOMAR-2026-07-29-000005", 2, msc=("68R10",),
                registered_at=_registered(9))
    served.database.commit("reclassify the newest")
    served.publish()

    assert "staging everything" in capsys.readouterr().out
    assert len(json.loads(
        (served.path / "subjects/msc/05C10.json").read_text()
    )["entries"]) == 2, "the page was left short"
    _agree(served)


def test_a_missing_year_document_rebuilds_rather_than_dropping_its_days(served, capsys):
    """Read from the pages up, a year document that had gone missing would be
    taken for an empty year, and the release would write one back naming only
    the day it touched. Every other day of that year would be gone from the
    only place they are named, and every page they name still there."""
    served.database.commit("a starting point")
    served.publish()
    (served.path / "browse/2026.json").unlink()
    served.database.add_entry("PALOMAR-2026-07-29-000002", 1)
    served.database.commit("add one")

    served.publish()

    assert "staging everything" in capsys.readouterr().out
    _agree(served)


def test_a_missing_recent_page_rebuilds_rather_than_starting_again(served, capsys):
    """For this one, "never published" and "gone" are distinguishable: it is
    written by every release, so the parent's record count says whether it
    should be there."""
    served.database.commit("a starting point")
    served.publish()
    (served.path / "recent.json").unlink()
    served.database.add_entry("PALOMAR-2026-07-29-000002", 1)
    served.database.commit("add one")

    served.publish()

    assert "staging everything" in capsys.readouterr().out
    _agree(served)


def test_an_old_incomplete_recent_shape_forces_a_rebuild(served, capsys):
    """An incremental release has no canonical records for untouched rows.

    Treating an old projection as patchable would add complete rows only for
    touched results and leave every other card in the retired shape forever.
    """
    served.database.commit("a starting point")
    served.publish()
    recent_path = served.path / "recent.json"
    recent = json.loads(recent_path.read_text())
    recent["entries"][0].pop("authors")
    recent_path.write_text(json.dumps(recent, indent=2, sort_keys=True) + "\n")
    served.database.add_entry("PALOMAR-2026-07-29-000002", 1)
    served.database.commit("add one")

    served.publish()

    assert "invalid recent.json" in capsys.readouterr().out
    assert served.delta["parent"] is None, "it patched rows from two contracts"
    _agree(served)


def test_incremental_only_staging_removes_partial_output_and_signals_full(
    served, tmp_path
):
    """The workflow may expand only after the incremental owner says it must."""
    served.database.commit("a starting point")
    served.publish()
    recent_path = served.path / "recent.json"
    recent_path.write_text("{}\n")
    served.database.add_entry("PALOMAR-2026-07-29-000002", 1)
    served.database.commit("add one")
    prior = served.fetched()
    output = tmp_path / "sparse-attempt"

    with pytest.raises(FullCheckoutRequired, match="invalid recent.json"):
        stage_public(
            served.database.path,
            output,
            previous=__import__("release_delta").base_of(served.delta),
            prior=prior,
            require_incremental=True,
        )

    assert not output.exists()


@pytest.mark.parametrize(
    ("relative", "field", "malformation"),
    [
        ("browse/index.json", "years", "missing"),
        ("browse/index.json", "years", "wrong type"),
        ("browse/index.json", None, "invalid JSON"),
        ("browse/index.json", None, "empty object"),
        ("browse/2026.json", "days", "missing"),
        ("browse/2026-07-29/1.json", "entries", "missing"),
        ("recent.json", "entries", "missing"),
        ("subjects/msc/05C10.json", "entries", "missing"),
        ("subjects/msc/05C10/2026.json", "days", "missing"),
        ("subjects/msc/05C10/2026-07-29/1.json", "entries", "missing"),
    ],
)
def test_a_malformed_prior_surface_forces_a_full_rebuild(
    served, capsys, relative, field, malformation
):
    """Every document an incremental release reads has one closed shape.

    These are the formerly unchecked collection fields, across the browse,
    recent and subject patchers. A missing field must not escape as KeyError
    or be treated as an empty collection: either would turn malformed served
    state into a red publication or a plausible but incomplete one.
    """
    _classified(
        served.database,
        "PALOMAR-2026-07-29-000002",
        msc=("05C10",),
        registered_at=_registered(2),
    )
    served.database.commit("a starting point")
    served.publish()
    path = served.path / relative
    if malformation == "invalid JSON":
        path.write_text("{")
    elif malformation == "empty object":
        path.write_text("{}\n")
    else:
        document = json.loads(path.read_text())
        if malformation == "missing":
            del document[field]
        else:
            document[field] = {}
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    _classified(
        served.database,
        "PALOMAR-2026-07-29-000003",
        msc=("05C10",),
        registered_at=_registered(3),
    )
    served.database.commit("touch every collection")

    served.publish()

    output = capsys.readouterr().out
    assert f"invalid {relative}" in output
    assert served.delta["parent"] is None
    _agree(served)


@pytest.mark.parametrize("level", [[], {}])
def test_an_unhashable_recent_trust_level_forces_a_full_rebuild(
    served, capsys, level
):
    """Every JSON value at the recent boundary fails as ValueError, not TypeError."""
    served.database.commit("a starting point")
    served.publish()
    recent_path = served.path / "recent.json"
    recent = json.loads(recent_path.read_text())
    recent["entries"][0]["trust"]["level"] = level
    recent_path.write_text(json.dumps(recent, indent=2, sort_keys=True) + "\n")
    served.database.add_entry("PALOMAR-2026-07-29-000002", 1)
    served.database.commit("add one")

    served.publish()

    assert "invalid recent.json" in capsys.readouterr().out
    assert served.delta["parent"] is None
    _agree(served)


def test_a_code_nobody_has_used_before_is_not_a_missing_document(served):
    """A code's front page exists as soon as any served result carries the
    code, so its absence means no served result does. Treating that as drift
    would make every publication that introduces a code a full rebuild, and
    early on that is most of them."""
    served.database.commit("a starting point")
    served.publish()
    _classified(served.database, "PALOMAR-2026-07-29-000002", 1,
                msc=("11Y99",), arxiv=("math.AG",), registered_at=_registered(9))
    served.database.commit("a code the registry has never used")

    site = served.publish()

    assert served.delta["parent"] is not None, "it fell back to a rebuild"
    assert (site / "subjects/msc/11Y99.json").is_file()
    _agree(served)


def test_the_plan_is_written_where_the_fetch_step_reads_it(served, tmp_path):
    """The contract between two workflow steps, which is a file of paths.

    Worth a test of its own because nothing else exercises it: the step that
    writes this has no credentials and the step that reads it does nothing
    else, so a mismatch here is a publication that patches against an empty
    directory and quietly rebuilds every time.
    """
    import subprocess
    import sys

    served.database.commit("a starting point")
    served.publish()
    parent = tmp_path / "parent.json"
    parent.write_bytes(
        __import__("release_delta").canonical_base_bytes(
            __import__("release_delta").base_of(served.delta)
        )
    )
    _classified(served.database, "PALOMAR-2026-07-29-000002", 1, registered_at=_registered(2))
    served.database.commit("add one")
    plan = tmp_path / "plan.txt"

    subprocess.run(
        [sys.executable, "tools/stage_public.py", "--root", str(served.database.path),
         "--previous", str(parent), "--plan", str(plan)],
        check=True, cwd=__import__("pathlib").Path(__file__).resolve().parents[1],
    )

    wanted = plan.read_text().splitlines()
    assert wanted == sorted(wanted)
    assert not any(
        line.startswith("/") or ".." in line.split("/") for line in wanted
    ), "a path the fetch step would refuse"
    for relative in ("recent.json", "browse/index.json", "browse/2026.json",
                     "browse/2026-07-29/1.json"):
        assert relative in wanted, wanted
        assert (served.path / relative).is_file()
    # A code no served result carries has no document to fetch, and the plan
    # names it anyway rather than working out which: what an absent one means
    # is the stager's decision, and the fetch step makes none.
    assert "subjects/msc/05C10.json" in wanted
    assert not (served.path / "subjects/msc/05C10.json").is_file()
