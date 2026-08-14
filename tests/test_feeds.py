"""The feeds, which are renderings of the JSON pages beside them.

A feed is built from `recent.json` and from each code's front page, and from
nothing else. That is what makes it impossible for the feed and the page under
one heading to name different results, and it is what stops a publication
reading every record a second time to describe what changed.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import pytest
from build_feeds import build_feeds
from stage_public import stage_public


def _feed(site, relative):
    return ET.parse(site / relative)


def test_main_and_category_feeds_are_generated(db, tmp_path):
    entry = db.entry_data("PALOMAR-2026-07-29-000002", 1)
    entry["classification"] = {
        "arxiv": ["math.CO", "cs.DM"],
        "msc2020": ["05C10"],
    }
    db.install_entry(entry)
    site = tmp_path / "site"

    stage_public(db.path, site)

    assert (site / "feed.xml").is_file()
    assert (site / "feeds/arxiv/math.CO.xml").is_file()
    assert (site / "feeds/msc/05C10.xml").is_file()
    assert len(_feed(site, "feed.xml").findall("./channel/item")) == 2
    category = _feed(site, "feeds/arxiv/math.CO.xml")
    assert [item.findtext("title") for item in category.findall("./channel/item")] == [
        "PALOMAR-2026-07-29-000002 version 1"
    ]


def test_feeds_include_only_the_latest_record_version(db, tmp_path):
    path = db.add_entry("PALOMAR-2026-07-29-000001", 2)
    latest = db.read_json(path.relative_to(db.path).as_posix())
    latest["registered_at"] = "2026-08-01T12:00:00Z"
    latest["abstract"] = '<img src=x onerror="alert(1)">'
    db.write_json(path.relative_to(db.path).as_posix(), latest)
    db.reindex()
    site = tmp_path / "site"

    stage_public(db.path, site)

    items = _feed(site, "feed.xml").findall("./channel/item")
    assert len(items) == 1
    assert "version=2" in items[0].findtext("guid")
    assert parsedate_to_datetime(items[0].findtext("pubDate")).isoformat() == "2026-08-01T12:00:00+00:00"
    assert items[0].findtext("description").startswith("&lt;img")


def test_a_feed_carries_the_rows_of_the_page_it_renders(db, tmp_path):
    """The property the whole arrangement exists for.

    While the feed worked out for itself which results were newest, it and the
    page under the same heading were two answers to one question. They agreed
    right up until one of them learned which of a record's timestamps says so,
    and the disagreement showed as a feed and a page naming different results,
    with neither wrong on its own terms.
    """
    entry = db.entry_data("PALOMAR-2026-07-29-000002", 1)
    entry["classification"] = {"arxiv": ["math.CO"], "msc2020": ["05C10"]}
    db.install_entry(entry)
    site = tmp_path / "site"

    stage_public(db.path, site)

    page = json.loads((site / "subjects/msc/05C10.json").read_text())
    feed = _feed(site, "feeds/msc/05C10.xml")
    assert [item.findtext("title") for item in feed.findall("./channel/item")] == [
        row["title"] for row in page["entries"]
    ]
    recent = json.loads((site / "recent.json").read_text())
    main = _feed(site, "feed.xml")
    assert [item.findtext("title") for item in main.findall("./channel/item")] == [
        row["title"] for row in recent["entries"]
    ]


def test_a_feed_reads_no_records_at_all(db, tmp_path):
    """The second whole-registry pass a publication used to make.

    Staging had already read every active record; the feed builder then read
    all of them again. Rendering the pages instead means the records may not
    even be there: an incremental release stages only the ones it added.
    """
    site = tmp_path / "site"
    stage_public(db.path, site)
    for path in (site / "entries").iterdir():
        path.unlink()

    build_feeds(site)

    assert _feed(site, "feed.xml").findall("./channel/item")


def test_retired_category_feed_remains_available(db, tmp_path):
    first = db.entry_data("PALOMAR-2026-07-29-000002", 1)
    first["classification"] = {"arxiv": ["math.CO"], "msc2020": ["05C10"]}
    db.install_entry(first)
    second_path = db.add_entry("PALOMAR-2026-07-29-000002", 2)
    second = db.read_json(second_path.relative_to(db.path).as_posix())
    second["classification"] = {"arxiv": ["cs.DM"], "msc2020": ["68R10"]}
    db.write_json(second_path.relative_to(db.path).as_posix(), second)
    db.reindex()
    site = tmp_path / "site"

    stage_public(db.path, site)

    assert _feed(site, "feeds/arxiv/math.CO.xml").findall("./channel/item") == []


def test_feed_replaces_xml_control_characters(db, tmp_path):
    """Put into the page rather than into the record, because the record's own
    schema refuses it. The feed builder still may not assume that: it renders
    whatever the page carries, and a document that reached XML with a control
    character in it is not a feed any aggregator can read."""
    site = tmp_path / "site"
    stage_public(db.path, site)
    recent = json.loads((site / "recent.json").read_text())
    recent["entries"][0]["title"] = "A\fcontrolled title"
    (site / "recent.json").write_text(json.dumps(recent))

    build_feeds(site)

    assert _feed(site, "feed.xml").findtext("./channel/item/title") == (
        "A\N{REPLACEMENT CHARACTER}controlled title"
    )


def test_a_feed_changes_only_when_its_items_do(db, tmp_path):
    """The property everything downstream rests on.

    lastBuildDate used to be the build time, so every feed's bytes changed on
    every publication whether or not anything in it had. No amount of change
    detection can help while that is true, because nothing is ever unchanged.
    """
    outputs = []
    for number in range(2):
        site = tmp_path / f"site{number}"
        stage_public(db.path, site)
        outputs.append(site)

    first, second = outputs
    written = sorted(path.relative_to(first).as_posix() for path in first.rglob("*.xml"))
    assert written, "no feeds were generated"
    for relative in written:
        assert (first / relative).read_bytes() == (second / relative).read_bytes(), (
            f"{relative} changed with nothing but the build time"
        )


def test_an_empty_feed_carries_no_build_date_to_change(db, tmp_path):
    """The same bug one level down.

    A code whose only classifier was superseded keeps its feed deliberately,
    and the build time was the only thing left in it that could move. So every
    empty category feed was rewritten whenever anybody registered anything --
    the whole classification vocabulary, for a result carrying a handful of
    codes.
    """
    first = db.entry_data("PALOMAR-2026-07-29-000002", 1)
    first["classification"] = {"arxiv": ["math.CO"], "msc2020": ["05C10"]}
    db.install_entry(first)
    second_path = db.add_entry("PALOMAR-2026-07-29-000002", 2)
    second = db.read_json(second_path.relative_to(db.path).as_posix())
    second["classification"] = {"arxiv": ["cs.DM"], "msc2020": ["68R10"]}
    db.write_json(second_path.relative_to(db.path).as_posix(), second)
    db.reindex()
    site = tmp_path / "site"

    stage_public(db.path, site)

    channel = _feed(site, "feeds/arxiv/math.CO.xml").find("./channel")
    assert channel.find("lastBuildDate") is None


# A feed is a notification channel, and is bounded like one.


def test_the_main_feed_stops_growing_with_the_registry(db, tmp_path, monkeypatch):
    """Unbounded, `feed.xml` is the whole registry: forty megabytes at a
    hundred thousand results, fetched by every reader on every poll."""
    monkeypatch.setattr("build_feeds.MAIN_FEED_ITEMS", 3)
    for serial in range(2, 9):
        db.add_entry(f"PALOMAR-2026-07-29-{serial:06d}", 1)
    site = tmp_path / "site"

    stage_public(db.path, site)

    assert len(_feed(site, "feed.xml").findall("./channel/item")) == 3


def test_a_category_feed_is_bounded_too(db, tmp_path, monkeypatch):
    """A popular MSC code is a sizeable fraction of the registry."""
    monkeypatch.setattr("build_feeds.CATEGORY_FEED_ITEMS", 2)
    for serial in range(2, 7):
        entry = db.entry_data(f"PALOMAR-2026-07-29-{serial:06d}", 1)
        entry["classification"] = {"arxiv": ["math.CO"], "msc2020": ["05C10"]}
        db.install_entry(entry)
    site = tmp_path / "site"

    stage_public(db.path, site)

    assert len(_feed(site, "feeds/msc/05C10.xml").findall("./channel/item")) == 2


def test_a_capped_feed_keeps_the_newest(db, tmp_path, monkeypatch):
    """Dropping the newest would make the cap worse than no feed at all."""
    monkeypatch.setattr("build_feeds.MAIN_FEED_ITEMS", 1)
    newest = db.entry_data("PALOMAR-2026-07-29-000009", 1, registered_at="2030-01-01T00:00:00Z")
    db.install_entry(newest)
    db.add_entry("PALOMAR-2026-07-29-000002", 1)
    site = tmp_path / "site"

    stage_public(db.path, site)

    items = _feed(site, "feed.xml").findall("./channel/item")
    assert len(items) == 1
    assert "PALOMAR-2026-07-29-000009" in items[0].findtext("link")


def test_a_staged_index_that_escapes_the_tree_is_refused(db, tmp_path):
    """Still checked, and now where the paths are actually read."""
    result = db.read_json("registrations/results/PALOMAR-2026-07-29-000001.json")
    result["versions"][0]["path"] = "../outside.json"
    db.write_json("registrations/results/PALOMAR-2026-07-29-000001.json", result)
    with pytest.raises(ValueError, match="path disagrees"):
        stage_public(db.path, tmp_path / "site")
