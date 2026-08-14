#!/usr/bin/env python3
"""The Palomar RSS feeds, as renderings of the JSON surfaces beside them.

A feed answers the same question as a page: `feed.xml` is `recent.json` and
`feeds/<kind>/<code>.xml` is that code's front page, in the format an
aggregator reads. So they are built from those documents and from nothing else.

They used to be built by reading every active record out of the database, which
was a second whole-registry pass on every publication, after the one staging
had already done. Worse, it was a second implementation of "which results are
newest": two answers to one question, which agree until one of them learns
which of a record's timestamps says so, and then show as a feed and a page
naming different results with neither wrong on its own terms. There is now one answer,
`tools/selection.py`, it is applied once when the pages are built, and the
feeds copy it.

Reading the previously published XML back and editing it would have been the
other way to make a feed cost what changed. It is not done: an RSS document is
a rendering, parsing one means writing a reader for whatever the last version
of this file emitted, and a rendering that is also a source of truth is a
format nobody can change.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import pathlib
import re
import xml.etree.ElementTree as ET
from email.utils import format_datetime
from typing import Any, Iterable

WEB_BASE = "https://palomar-registry.org/"
FEED_BASE = "https://data.palomar-registry.org/"
XML_INVALID_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# A feed is a notification channel, not the registry. Without a bound, the main
# feed carries every result ever accepted -- 40 MB at a hundred thousand of them,
# fetched by every reader on every poll -- and a popular MSC code carries a
# sizeable fraction of that. Readers want what is new; the registry is what is
# for browsing. These are the numbers most aggregators are comfortable with, and
# they are also the sizes of the two documents these are rendered from.
MAIN_FEED_ITEMS = 200
CATEGORY_FEED_ITEMS = 50


def _rfc2822(value: str) -> str:
    if len(value) == 10:
        parsed = dt.datetime.fromisoformat(value).replace(tzinfo=dt.UTC)
    else:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return format_datetime(parsed)


def _xml_text(value: object, *, rss_html: bool = False) -> str:
    clean = XML_INVALID_RE.sub("\N{REPLACEMENT CHARACTER}", str(value))
    return html.escape(clean) if rss_html else clean


def _feed(
    rows: list[dict[str, Any]],
    *,
    title: str,
    description: str,
    feed_url: str,
) -> bytes:
    ET.register_namespace("atom", "http://www.w3.org/2005/Atom")
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = title
    ET.SubElement(channel, "link").text = WEB_BASE
    ET.SubElement(channel, "description").text = description
    ET.SubElement(channel, "language").text = "en"
    # The last time this channel's content changed, which is what RSS asks for
    # and the only way a feed's bytes can be a function of its items. A build
    # time here meant every feed changed on every publication whether or not
    # anything in it had, so nothing downstream could tell an unchanged feed
    # from a changed one.
    #
    # A feed with nothing in it carries no lastBuildDate at all, rather than
    # the time the registry was last generated. That fallback was the same bug
    # one level down: a code whose only classifier was superseded keeps a feed
    # deliberately, and its one variable field moved on every publication, so
    # every empty category feed was rewritten every time anybody registered
    # anything -- which is the whole classification vocabulary, thousands of
    # objects, for a result classified under a handful of codes.
    if rows:
        ET.SubElement(channel, "lastBuildDate").text = _rfc2822(
            max(str(row["published_at"]) for row in rows)
        )
    ET.SubElement(
        channel,
        "{http://www.w3.org/2005/Atom}link",
        {"href": feed_url, "rel": "self", "type": "application/rss+xml"},
    )
    for row in rows:
        url = f"{WEB_BASE}entry.html?id={row['id']}&version={row['version']}"
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = _xml_text(row["title"])
        ET.SubElement(item, "link").text = url
        ET.SubElement(item, "guid", {"isPermaLink": "true"}).text = url
        # RSS descriptions are commonly rendered as entity-encoded HTML. Escape
        # once here and let ElementTree escape the entities again in XML so a
        # reader's HTML pass still sees literal untrusted submission text.
        ET.SubElement(item, "description").text = _xml_text(row["abstract"], rss_html=True)
        ET.SubElement(item, "pubDate").text = _rfc2822(str(row["published_at"]))
        classification = row.get("classification") or {}
        for code in classification.get("arxiv", []):
            ET.SubElement(item, "category", {"domain": "arxiv"}).text = code
        for code in classification.get("msc2020", []):
            ET.SubElement(item, "category", {"domain": "msc2020"}).text = code
    ET.indent(rss, space="  ")
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


def _rows(site: pathlib.Path, relative: str) -> list[dict[str, Any]]:
    document = json.loads((site / relative).read_text(encoding="utf-8"))
    return list(document["entries"])


def write_main_feed(site: pathlib.Path) -> pathlib.Path:
    target = site / "feed.xml"
    target.write_bytes(
        _feed(
            _rows(site, "recent.json")[:MAIN_FEED_ITEMS],
            title="Palomar accepted results",
            description="New and updated Lean-verified results accepted by Palomar.",
            feed_url=f"{FEED_BASE}feed.xml",
        )
    )
    return target


def write_category_feed(site: pathlib.Path, kind: str, code: str) -> pathlib.Path:
    label = "arXiv" if kind == "arxiv" else "MSC2020"
    relative = f"feeds/{kind}/{code}.xml"
    target = site / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(
        _feed(
            _rows(site, f"subjects/{kind}/{code}.json")[:CATEGORY_FEED_ITEMS],
            title=f"Palomar — {label} {code}",
            description=f"Lean-verified Palomar results classified under {label} {code}.",
            feed_url=f"{FEED_BASE}{relative}",
        )
    )
    return target


def staged_codes(site: pathlib.Path) -> list[tuple[str, str]]:
    """Every code whose front page this staging run has written.

    The front pages are the files directly under `subjects/<kind>/`; a code's
    archive pages are in the directory beside its front page and are not feeds.
    """
    found: list[tuple[str, str]] = []
    for kind in ("arxiv", "msc"):
        for path in sorted((site / "subjects" / kind).glob("*.json")):
            found.append((kind, path.stem))
    return found


def build_feeds(
    site: pathlib.Path,
    codes: Iterable[tuple[str, str]] | None = None,
) -> list[pathlib.Path]:
    """Render the feeds for the surfaces this release staged.

    `codes` is which category feeds to write; `None` means every code with a
    staged front page, which is what a full rebuild wants. An incremental
    release names the handful it touched, because the classification vocabulary
    is thousands of codes and a result carries a few of them.
    """
    for kind in ("arxiv", "msc"):
        (site / "feeds" / kind).mkdir(parents=True, exist_ok=True)
    written = [write_main_feed(site)]
    for kind, code in staged_codes(site) if codes is None else sorted(codes):
        written.append(write_category_feed(site, kind, code))
    return written
