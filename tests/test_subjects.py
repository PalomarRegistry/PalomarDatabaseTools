"""What is new under one classification code, as a page rather than a filter
applied to the whole registry in a browser.

The selection is the feeds' selection, read from the same module, so the two
cannot come to name different results under one heading. The page is capped at
fifty and keeps the newest, which is truncation on purpose: browsing is the
surface that lists everything.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest
import release_delta
from build_subjects import SUBJECT_PAGE_ITEMS, build_subjects
from stage_public import stage_public

FIRST = "PALOMAR-2026-07-29-000001"


def _page(site, kind, code):
    return json.loads((site / "subjects" / kind / f"{code}.json").read_text())


def _rewritten(previous, delta, prefix):
    """Which staged objects changed bytes, and by how many bytes.

    The publisher writes every stable object the release stages. Comparing its
    digest with the parent separately isolates whether a bounded page quietly
    grew with the registry.
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


def _classified(db, identifier, version=1, arxiv=("math.CO",), msc=("05C10",), **overrides):
    entry = db.entry_data(identifier, version)
    entry["classification"] = {"arxiv": list(arxiv), "msc2020": list(msc)}
    entry.update(overrides)
    db.install_entry(entry)
    return entry


def test_a_page_names_the_results_carrying_its_code(db, tmp_path):
    _classified(db, "PALOMAR-2026-07-29-000002")
    site = tmp_path / "release"
    stage_public(db.path, site)

    page = _page(site, "msc", "05C10")
    assert page["schema_version"] == 2
    assert page["kind"] == "msc"
    assert page["code"] == "05C10"
    assert [row["id"] for row in page["entries"]] == ["PALOMAR-2026-07-29-000002"]
    assert _page(site, "arxiv", "math.CO")["entries"][0]["id"] == "PALOMAR-2026-07-29-000002"


def test_a_row_carries_what_a_reader_needs_to_check_the_page(db, tmp_path):
    """The claim is "the newest results under this code". A row that carried
    neither its date nor its codes would leave both halves of that unverifiable
    on the other side, and a result shown under the wrong heading is a well
    formed row."""
    entry = _classified(db, "PALOMAR-2026-07-29-000002")
    site = tmp_path / "release"
    stage_public(db.path, site)

    row = _page(site, "msc", "05C10")["entries"][0]
    index = json.loads((site / "index.json").read_text())
    summary = next(item for item in index["entries"] if item["id"] == entry["id"])
    assert {key: row[key] for key in summary} == summary
    assert row["published_at"] == entry["registered_at"]
    assert row["classification"] == {"arxiv": ["math.CO"], "msc2020": ["05C10"]}


def test_only_the_current_version_of_a_result_appears(db, tmp_path):
    _classified(db, "PALOMAR-2026-07-29-000002", 1)
    _classified(db, "PALOMAR-2026-07-29-000002", 2)
    site = tmp_path / "release"
    stage_public(db.path, site)

    assert [row["version"] for row in _page(site, "msc", "05C10")["entries"]] == [2]


def test_a_page_and_the_feed_for_the_same_code_name_the_same_results(db, tmp_path):
    """The reason the selection is one module and not two.

    A page and a feed that disagreed would each be defensible on its own terms,
    and a reader would have no way to tell which one was the registry.
    """
    for serial in range(2, 6):
        _classified(
            db,
            f"PALOMAR-2026-07-29-{serial:06d}",
            registered_at=f"2026-08-0{serial}T00:00:00Z",
        )
    site = tmp_path / "release"
    stage_public(db.path, site)

    feed = ET.parse(site / "feeds/msc/05C10.xml")
    links = [item.findtext("link") for item in feed.findall("./channel/item")]
    page = [
        f"https://palomar-registry.org/entry.html?id={row['id']}&version={row['version']}"
        for row in _page(site, "msc", "05C10")["entries"]
    ]
    assert page == links


def test_a_retired_code_keeps_answering(db, tmp_path):
    """The same reasoning as the feeds: a subscription, a bookmark or a link
    should not start returning 404 because someone published a v2."""
    _classified(db, "PALOMAR-2026-07-29-000002", 1, arxiv=("math.CO",), msc=("05C10",))
    _classified(db, "PALOMAR-2026-07-29-000002", 2, arxiv=("cs.DM",), msc=("68R10",))
    site = tmp_path / "release"
    stage_public(db.path, site)

    assert _page(site, "msc", "05C10")["entries"] == []
    assert [row["id"] for row in _page(site, "msc", "68R10")["entries"]] == [
        "PALOMAR-2026-07-29-000002"
    ]


def test_a_code_everything_carries_is_capped_and_keeps_the_newest(db, tmp_path, monkeypatch):
    """The adversarial distribution for this surface: every result under one
    code. Dropping the newest would make the cap worse than no page at all."""
    monkeypatch.setattr("build_subjects.SUBJECT_PAGE_ITEMS", 2)
    for serial in range(2, 8):
        _classified(
            db,
            f"PALOMAR-2026-07-29-{serial:06d}",
            registered_at=f"2026-08-0{serial}T00:00:00Z",
        )
    site = tmp_path / "release"
    stage_public(db.path, site)

    page = _page(site, "msc", "05C10")["entries"]
    assert len(page) == 2
    assert [row["id"] for row in page] == [
        "PALOMAR-2026-07-29-000007",
        "PALOMAR-2026-07-29-000006",
    ]


def test_the_cap_is_the_one_the_category_feed_uses():
    """Two numbers for one product decision is how a page and its feed come to
    show different amounts of the same thing."""
    from build_feeds import CATEGORY_FEED_ITEMS

    assert SUBJECT_PAGE_ITEMS == CATEGORY_FEED_ITEMS == 50


def test_adding_one_record_rewrites_only_the_pages_for_its_own_codes(repo, tmp_path):
    """The growth class. Measured as the bytes the publisher uploads, because
    a page that carried anything derived from the whole registry would rewrite
    every code in the vocabulary, which is a constant number of objects and a
    growing number of bytes."""
    paths, written = {}, {}
    for size in (2, 20):
        for serial in range(1, size):
            _classified(
                repo,
                f"PALOMAR-2026-07-28-{serial:06d}",
                arxiv=(f"cs.{chr(65 + serial % 20)}{chr(65 + serial // 20)}",),
                msc=(f"{serial % 90 + 10:02d}A05",),
            )
        repo.commit(f"a registry of {size}")
        previous = _stage(repo.path, tmp_path / f"base{size}")
        newcomer = repo.next_identifier()
        _classified(repo, newcomer, arxiv=("math.AG",), msc=("14Q05",))
        repo.commit("one more")
        delta = _stage(repo.path, tmp_path / f"next{size}", previous=previous)
        paths[size], written[size] = _rewritten(previous, delta, "subjects/")
        repo.remove(f"entries/{newcomer}-v1.json")
        repo.reindex()

    assert written[2] == written[20], written
    assert paths[2] == paths[20] == [
        "subjects/arxiv/math.AG.json",
        "subjects/arxiv/math.AG/2026-07-29/1.json",
        "subjects/arxiv/math.AG/2026.json",
        "subjects/msc/14Q05.json",
        "subjects/msc/14Q05/2026-07-29/1.json",
        "subjects/msc/14Q05/2026.json",
    ]


def test_a_popular_code_costs_the_cap_and_not_the_registry(repo, tmp_path, monkeypatch):
    """The same measurement for the concentrated case: every record under one
    code, so every publication rewrites that code's front page, one archive
    page and the year document over it. What must not grow is what any of them
    weighs.

    Eleven and ninety-one, so that every count in those documents has the same
    number of decimal digits in both registries. A count is the only thing here
    that grows at all, and it grows by a digit for every tenfold registry.
    """
    monkeypatch.setattr("build_subjects.SUBJECT_PAGE_ITEMS", 2)
    written = {}
    for size in (11, 91):
        for serial in range(1, size):
            _classified(repo, f"PALOMAR-2026-07-28-{serial:06d}")
        repo.commit(f"a registry of {size}")
        previous = _stage(repo.path, tmp_path / f"base{size}")
        newcomer = repo.next_identifier()
        _classified(
            repo,
            newcomer,
            registered_at="2030-01-01T00:00:00Z",
        )
        repo.commit("one more")
        delta = _stage(repo.path, tmp_path / f"next{size}", previous=previous)
        _paths, written[size] = _rewritten(previous, delta, "subjects/")
        repo.remove(f"entries/{newcomer}-v1.json")
        repo.reindex()

    assert written[11] == written[91], written


def test_a_code_a_page_cannot_be_named_for_is_refused(db, tmp_path):
    """A code that is not a code is a path, and a path in a filename is a way
    out of the directory it was supposed to be written into.

    The published schema refuses this first, so a staged release never reaches
    here. The pure builder still owns the filename grammar and refuses unsafe
    input directly, so bypassing upstream validation cannot write outside the
    output directory.
    """
    entry = db.entry_data("PALOMAR-2026-07-29-000002", 1)
    entry["classification"] = {"arxiv": ["math.CO"], "msc2020": ["../../etc/passwd"]}
    output = tmp_path / "site"
    output.mkdir()

    with pytest.raises(ValueError, match="invalid MSC2020 category"):
        build_subjects(output, [entry], [{"id": entry["id"], "version": 1}])


def test_the_page_carries_the_index_row_for_the_version_it_names(db, tmp_path):
    """Not the row for whatever else shares the identifier: a page names exact
    versions, and taking the title from the wrong one would show a superseded
    statement under a current result's link."""
    _classified(db, "PALOMAR-2026-07-29-000002", 1)
    _classified(db, "PALOMAR-2026-07-29-000002", 2)
    site = tmp_path / "release"
    stage_public(db.path, site)

    row = _page(site, "msc", "05C10")["entries"][0]
    assert row["path"] == "entries/PALOMAR-2026-07-29-000002-v2.json"
    assert row["title"] == "PALOMAR-2026-07-29-000002 version 2"


def test_build_subjects_needs_no_reads_of_its_own(db, tmp_path):
    """It is handed the records the stager already read. A surface that opened
    every record again would put back a whole pass over the registry."""
    summaries = db.summaries()
    entries = [db.read_json(summary["path"]) for summary in summaries]
    output = tmp_path / "site"
    output.mkdir()

    written = build_subjects(output, entries, summaries)

    assert sorted(path.relative_to(output).as_posix() for path in written) == [
        "subjects/arxiv/math.NT.json",
        "subjects/arxiv/math.NT/2026-07-29/1.json",
        "subjects/arxiv/math.NT/2026.json",
        "subjects/msc/11N13.json",
        "subjects/msc/11N13/2026-07-29/1.json",
        "subjects/msc/11N13/2026.json",
    ]
    assert [row["id"] for row in _page(output, "msc", "11N13")["entries"]] == [FIRST]


# The archive under a code, which is what the front page is not.


def test_the_archive_carries_every_current_version_under_the_code(db, tmp_path, monkeypatch):
    """The front page is fifty rows of what is new. Something has to list the
    rest, and before this the only thing that did was `index.json` filtered in
    a browser: the whole registry for a page of a code."""
    monkeypatch.setattr("build_subjects.SUBJECT_PAGE_ITEMS", 2)
    for serial in range(2, 8):
        _classified(db, f"PALOMAR-2026-07-29-{serial:06d}")
    site = tmp_path / "release"
    stage_public(db.path, site)

    archive = json.loads((site / "subjects/msc/05C10/2026-07-29/1.json").read_text())
    assert [row["id"] for row in archive["entries"]] == [
        f"PALOMAR-2026-07-29-{serial:06d}" for serial in range(2, 8)
    ]
    assert len(_page(site, "msc", "05C10")["entries"]) == 2


def test_the_front_page_is_also_the_head_of_the_archive(db, tmp_path):
    """One object, because it changes exactly when the code changes. Two would
    have been two things to keep in step for no reader's benefit."""
    _classified(db, "PALOMAR-2026-07-29-000002")
    site = tmp_path / "release"
    stage_public(db.path, site)

    head = _page(site, "msc", "05C10")
    assert head["results"] == 1 and head["versions"] == 1
    assert head["years"] == [{"year": "2026", "days": 1, "results": 1, "versions": 1}]


def test_the_archive_is_paged_by_the_identifier_and_not_by_position(db, tmp_path):
    """So that a result joining a code does not move any row already under it.

    A page filled in join order would have to renumber, or keep a directory
    saying which page holds which result, and a directory is O(pages) rewritten
    whenever the code changes.
    """
    _classified(db, "PALOMAR-2026-07-29-000201")
    _classified(db, "PALOMAR-2026-08-04-000001")
    site = tmp_path / "release"
    stage_public(db.path, site)

    assert (site / "subjects/msc/05C10/2026-07-29/2.json").is_file()
    assert (site / "subjects/msc/05C10/2026-08-04/1.json").is_file()
    assert [row["day"] for row in json.loads(
        (site / "subjects/msc/05C10/2026.json").read_text()
    )["days"]] == ["2026-07-29", "2026-08-04"]


def test_a_code_names_the_path_of_its_own_archive_and_not_the_registry_s(db, tmp_path):
    """A code's archive is the same layout under a different directory, so the
    templates that make it traversable have to be that code's own.

    Deriving them from the directory the collection is being written into is
    what makes that true without anything here choosing it, which is the point:
    a template chosen separately from the writer is a second statement of where
    the documents go, and the two can disagree.
    """
    _classified(db, "PALOMAR-2026-07-29-000201")
    site = tmp_path / "release"
    stage_public(db.path, site)

    head = _page(site, "msc", "05C10")
    assert head["year_path"] == "subjects/msc/05C10/{year}.json"
    year = json.loads((site / head["year_path"].format(year="2026")).read_text())
    assert year["page_path"] == "subjects/msc/05C10/{day}/{page}.json"
    reached = json.loads(
        (site / year["page_path"].format(day="2026-07-29", page=2)).read_text()
    )
    assert [row["id"] for row in reached["entries"]] == ["PALOMAR-2026-07-29-000201"]


def test_a_version_that_changes_its_codes_leaves_one_archive_and_joins_another(db, tmp_path):
    """One page out of each, and neither of them found by looking anything up:
    both are the page the result's identifier names under that code."""
    _classified(db, "PALOMAR-2026-07-29-000002", 1, msc=("05C10",))
    _classified(db, "PALOMAR-2026-07-29-000002", 2, msc=("68R10",))
    site = tmp_path / "release"
    stage_public(db.path, site)

    assert json.loads(
        (site / "subjects/msc/05C10/2026-07-29/1.json").read_text()
    )["entries"] == []
    assert [row["version"] for row in json.loads(
        (site / "subjects/msc/68R10/2026-07-29/1.json").read_text()
    )["entries"]] == [2]


def test_the_front_page_carries_the_abstract_and_the_archive_does_not(db, tmp_path):
    """The feed for a code is a rendering of the front page, so what a feed
    item needs has to be on it. Repeating fifty abstracts down every archive
    page would be the size of the registry again for a field nothing there
    renders."""
    entry = _classified(db, "PALOMAR-2026-07-29-000002")
    site = tmp_path / "release"
    stage_public(db.path, site)

    assert _page(site, "msc", "05C10")["entries"][0]["abstract"] == entry["abstract"]
    archive = json.loads((site / "subjects/msc/05C10/2026-07-29/1.json").read_text())
    assert "abstract" not in archive["entries"][0]
