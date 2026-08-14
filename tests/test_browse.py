"""Browsing the registry a page at a time, so that adding one record does not
rewrite the list of all of them.

A page is the results accepted on one day whose serials fall in one band of
two hundred. Both halves of that are read from the identifier, so a page is
decided once at registration and never moves. That day is the acceptance date
and not the day of registration, which waits on the submitter consenting, so a
result can be appended to a day already past. The tests below build the
concentrated cases -- a whole registry on one day, a day that overflows a page,
a result whose later versions arrive years later -- because a layout that only
holds for a well-spread registry is not a layout.
"""

from __future__ import annotations

import json

import day_pages
import pytest
import release_delta
from build_browse import HEAD_PATH
from day_pages import PAGE_SERIALS, coordinate
from stage_public import stage_public

FIRST = "PALOMAR-2026-07-29-000001"


def _page(site, day, number):
    return json.loads((site / "browse" / day / f"{number}.json").read_text())


def _year(site, year):
    return json.loads((site / "browse" / f"{year}.json").read_text())


def _head(site):
    return json.loads((site / HEAD_PATH).read_text())


def _rewritten(previous, delta, prefix):
    """Which staged objects changed bytes, and by how many bytes.

    The publisher writes every stable object the release stages. Comparing its
    digest with the parent separately isolates payload growth: a document that
    carried the size of the registry would still be one object, but its bytes
    would grow.
    """
    prior = {row["path"]: row["sha256"] for row in previous["stable"]}
    changed = [
        row
        for row in delta["stable"]
        if row["path"].startswith(prefix) and prior.get(row["path"]) != row["sha256"]
    ]
    return sorted(row["path"] for row in changed), sum(row["bytes"] for row in changed)


def _stage(root, site, **arguments):
    if arguments.get("previous") is not None:
        arguments["previous"] = release_delta.base_of(arguments["previous"])
    stage_public(root, site, **arguments)
    return release_delta.parse((site / "release-delta.json").read_bytes())


def test_a_page_holds_the_results_of_one_day_whose_serials_share_a_band(db, tmp_path):
    db.add_entry("PALOMAR-2026-07-29-000199", 1)
    db.add_entry("PALOMAR-2026-07-29-000201", 1)
    db.add_entry("PALOMAR-2026-07-30-000001", 1)
    site = tmp_path / "release"
    stage_public(db.path, site)

    assert [row["id"] for row in _page(site, "2026-07-29", 1)["entries"]] == [
        FIRST,
        "PALOMAR-2026-07-29-000199",
    ]
    assert [row["id"] for row in _page(site, "2026-07-29", 2)["entries"]] == [
        "PALOMAR-2026-07-29-000201"
    ]
    assert [row["id"] for row in _page(site, "2026-07-30", 1)["entries"]] == [
        "PALOMAR-2026-07-30-000001"
    ]


def test_a_page_says_which_day_and_band_it_is(db, tmp_path):
    """A page fetched on its own has to be checkable against the URL it came
    from, or a reader has no way to notice it was served the wrong one."""
    site = tmp_path / "release"
    stage_public(db.path, site)

    page = _page(site, "2026-07-29", 1)
    assert (page["schema_version"], page["day"], page["page"]) == (1, "2026-07-29", 1)


def test_the_rows_are_the_ones_the_index_carries(db, tmp_path):
    """So the existing row grammar, path check and record URL all apply
    unchanged; what is new is which rows a page claims to cover."""
    db.add_entry("PALOMAR-2026-07-29-000002", 1)
    site = tmp_path / "release"
    stage_public(db.path, site)

    index = json.loads((site / "index.json").read_text())
    everything = [
        row
        for path in sorted((site / "browse").rglob("*/*.json"))
        for row in json.loads(path.read_text())["entries"]
    ]
    assert sorted(everything, key=lambda row: row["path"]) == sorted(
        index["entries"], key=lambda row: row["path"]
    )


def test_every_version_of_one_result_shares_a_page(db, tmp_path):
    """A page is read from the identifier alone, so a result's history is never
    split across two pages and a new version never moves an old one.

    This is also the case that decided the layout. A new version reuses the
    first version's identifier, so a v2 registered years later belongs to the
    day its v1 was: paging on anything the later version carries would move a
    row out of a page a reader has already been pointed at.

    `registered_at` is exactly such a thing. Every ordering surface reads it, a
    later version carries a later one, and browsing must go on reading the
    identifier -- so these versions are registered a year and two years after
    the day this page is for.
    """
    db.add_entry(FIRST, 2, registered_at="2027-07-29T00:00:00Z")
    db.add_entry(FIRST, 3, registered_at="2028-07-29T00:00:00Z")
    site = tmp_path / "release"
    stage_public(db.path, site)

    year, day, number = coordinate(FIRST)
    assert [row["version"] for row in _page(site, day, number)["entries"]] == [1, 2, 3]


def test_the_year_document_names_every_day_and_the_pages_that_day_uses(db, tmp_path):
    db.add_entry("PALOMAR-2026-07-29-000201", 1)
    db.add_entry("PALOMAR-2026-08-03-000001", 2)
    db.add_entry("PALOMAR-2026-08-03-000001", 1)
    site = tmp_path / "release"
    stage_public(db.path, site)

    year = _year(site, "2026")
    assert year["schema_version"] == 1 and year["year"] == "2026"
    assert year["days"] == [
        {
            "day": "2026-07-29",
            "first_page": 1,
            "last_page": 2,
            "results": 2,
            "versions": 2,
        },
        {
            "day": "2026-08-03",
            "first_page": 1,
            "last_page": 1,
            "results": 1,
            "versions": 2,
        },
    ]


def test_the_head_names_the_years_and_never_the_days_or_the_pages(db, tmp_path):
    """The bound the whole layout rests on.

    A document naming every day is O(days), and at any bounded rate of
    submission the number of days grows with the registry, so that is O(S)
    rewritten once per accepted result. A document naming every page is worse.
    Years arrive at one row each, which is the one thing here that grows with
    time rather than with the change.
    """
    db.add_entry("PALOMAR-2027-01-04-000001", 1)
    site = tmp_path / "release"
    stage_public(db.path, site)

    head = _head(site)
    assert head["results"] == 2 and head["versions"] == 2
    assert head["years"] == [
        {"year": "2026", "days": 1, "results": 1, "versions": 1},
        {"year": "2027", "days": 1, "results": 1, "versions": 1},
    ]
    assert "days" not in head and "pages" not in head


def test_a_quiet_day_has_no_row_and_no_page(db, tmp_path):
    db.add_entry("PALOMAR-2026-08-03-000001", 1)
    site = tmp_path / "release"
    stage_public(db.path, site)

    assert [row["day"] for row in _year(site, "2026")["days"]] == [
        "2026-07-29",
        "2026-08-03",
    ]
    assert not (site / "browse" / "2026-07-30").exists()


def test_a_withdrawn_version_leaves_its_page(db, tmp_path):
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

    _year_, day, number = coordinate(FIRST)
    assert [row["version"] for row in _page(site, day, number)["entries"]] == [2]
    assert _year(site, "2026")["days"][0]["versions"] == 1


def test_adding_one_record_rewrites_one_page_and_the_two_documents_above_it(repo, tmp_path):
    """The growth class, and the whole reason browsing is paged at all.

    Measured as the staged stable objects whose bytes changed. The publisher
    writes every staged stable object; this digest comparison isolates whether
    the payload itself grows with the registry.

    Eleven and ninety-one rather than ten and a hundred, so that every count in
    the two documents above the pages has the same number of decimal digits in
    both registries. A count is the only thing in this layout that grows at
    all, and it grows by a digit every time the registry grows tenfold; without
    choosing the sizes, this would be asserting that ten times the registry
    costs the same to within one byte, which is true and reads like an accident.
    """
    paths, written = {}, {}
    for size in (11, 91):
        for serial in range(1, size):
            repo.add_entry(f"PALOMAR-2026-07-28-{serial:06d}", 1)
        repo.commit(f"a registry of {size}")
        previous = _stage(repo.path, tmp_path / f"base{size}")
        newcomer = repo.next_identifier()
        repo.add_entry(newcomer, 1)
        repo.commit("one more")
        delta = _stage(repo.path, tmp_path / f"next{size}", previous=previous)
        paths[size], written[size] = _rewritten(previous, delta, "browse/")
        repo.remove(f"entries/{newcomer}-v1.json")
        repo.reindex()

    assert written[11] == written[91], written
    assert paths[11] == paths[91] == [
        "browse/2026-07-29/1.json",
        "browse/2026.json",
        HEAD_PATH,
    ]


def test_a_result_accepted_on_a_day_already_past_still_costs_one_page(repo, tmp_path):
    """A day is not sealed by having passed.

    The day in an identifier is the day the review accepted the result, and
    registration waits on the submitter reading that review and consenting, so
    a result accepted on the twenty-ninth can be registered after one accepted
    on the fifth of the following month and append to the earlier day's page.
    What this layout rests on is not that appends only ever touch today; it is
    that one write touches one page, which an append to a past day does.
    """
    repo.add_entry("PALOMAR-2026-07-29-000001", 1, accepted_at="2026-07-29")
    repo.add_entry("PALOMAR-2026-08-05-000001", 1, accepted_at="2026-08-05")
    repo.commit("two days")
    previous = _stage(repo.path, tmp_path / "base")

    repo.add_entry("PALOMAR-2026-07-29-000002", 1, accepted_at="2026-07-29")
    repo.commit("registered later, under the earlier day")
    delta = _stage(repo.path, tmp_path / "next", previous=previous)

    paths, _ = _rewritten(previous, delta, "browse/")
    assert paths == ["browse/2026-07-29/1.json", "browse/2026.json", HEAD_PATH]
    site = tmp_path / "next"
    assert [row["id"] for row in _page(site, "2026-07-29", 1)["entries"]] == [
        "PALOMAR-2026-07-29-000001",
        "PALOMAR-2026-07-29-000002",
    ]
    assert [day["day"] for day in _year(site, "2026")["days"]] == [
        "2026-07-29",
        "2026-08-05",
    ]


def test_a_registry_registered_on_one_day_is_still_pages_of_two_hundred(db, tmp_path):
    """The adversarial distribution: every result on one day.

    That is the one the previous layout could not have: a hundred fixed shards
    would have put four hundred results in four rows of a hundred pages each,
    every one of them rewritten by the next result. Here it is two pages, and
    the second one is the only one a new result touches.
    """
    for serial in range(1, 401):
        db.add_entry(f"PALOMAR-2026-08-07-{serial:06d}", 1)
    site = tmp_path / "release"
    stage_public(db.path, site)

    day = _year(site, "2026")["days"][-1]
    assert (day["day"], day["first_page"], day["last_page"]) == ("2026-08-07", 1, 2)
    assert len(_page(site, "2026-08-07", 1)["entries"]) == PAGE_SERIALS
    assert len(_page(site, "2026-08-07", 2)["entries"]) == 400 - PAGE_SERIALS


def test_a_page_cannot_hold_more_results_than_the_band_it_covers(db, tmp_path):
    """Structural, not a checked limit. A page that could refuse would have no
    remedy: a day cannot be repaged once readers have been pointed at it, so
    the bound has to be one nothing can exceed rather than one that stops a
    publication."""
    for serial in range(1, 250):
        db.add_entry(f"PALOMAR-2026-08-07-{serial:06d}", 1)
    site = tmp_path / "release"
    stage_public(db.path, site)

    for path in (site / "browse" / "2026-08-07").iterdir():
        page = json.loads(path.read_text())
        assert len({row["id"] for row in page["entries"]}) <= PAGE_SERIALS


def test_an_identifier_that_cannot_be_placed_is_refused_rather_than_guessed_at():
    """A guessed page puts a record where no reader will look, and the record
    is then absent from browsing while every other surface has it."""
    with pytest.raises(ValueError, match="cannot page"):
        coordinate("PALOMAR-2026-07-29-0001")
    with pytest.raises(ValueError, match="cannot page"):
        coordinate("PALOMAR-2026-07-29-000001-v1")


def test_the_placement_rule_is_read_from_the_identifier_and_nothing_else():
    assert coordinate("PALOMAR-2026-07-29-000001") == ("2026", "2026-07-29", 1)
    assert coordinate("PALOMAR-2026-07-29-000200") == ("2026", "2026-07-29", 1)
    assert coordinate("PALOMAR-2026-07-29-000201") == ("2026", "2026-07-29", 2)
    assert day_pages.PAGE_SERIALS == 200
