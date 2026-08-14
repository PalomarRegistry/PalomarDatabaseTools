#!/usr/bin/env python3
"""What is under one classification code: a front page, and pages of the rest.

Before subject pages existed, a reader who wanted the results classified 11N13
could subscribe to the RSS feed or fetch the retired whole-registry index and
every record it named, then filter them in a browser. That historical second
path cost the whole registry for a page of about fifty rows; public
`/index.json` is now a 404.

`subjects/<kind>/<code>.json` is the front page: the newest fifty current
versions carrying the code, newest first. It is kept, and it is now also the
head of the code's archive, because everything else here needs a document that
changes whenever the code changes and there is no reason for that to be two
objects. Keeping it is what `feeds/<kind>/<code>.xml` is rendered from, so the
feed and the page cannot name different results under one heading -- which they
could while each worked the selection out for itself, and each would have been
defensible on its own terms.

Fifty is a page of what is new under a code, not the code's archive. The
archive is `subjects/<kind>/<code>/<YYYY-MM-DD>/<page>.json`, paged exactly as
browsing is, by the day the result was registered and the band its serial falls
in. That is a pure function of the identifier, so a result joining a code
appends to one page, a new version of a result already under the code rewrites
the one page its identifier names, and a version that drops the code takes its
row out of that same page. None of those has to consult a directory to find
out which page it is, which is the point: a directory naming every page of a
code is O(pages) rewritten whenever the code changes, and a code can hold a
sizeable fraction of the registry.
"""

from __future__ import annotations

import pathlib
from typing import Any

import day_pages
from selection import by_code, code_seeds, latest_entries, published_at

SCHEMA_VERSION = day_pages.SCHEMA_VERSION
# Truncation, and deliberately so: this is a page of what is new under a code,
# not the code's archive, and a popular MSC code is a sizeable fraction of the
# registry. The cap keeps the newest, because a page that dropped those would
# be worse than no page at all. The archive pages are what list everything.
SUBJECT_PAGE_ITEMS = 50


def head_path(kind: str, code: str) -> str:
    return f"subjects/{kind}/{code}.json"


def directory(kind: str, code: str) -> str:
    return f"subjects/{kind}/{code}"


def archive_row(entry: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    """One line of a subject page.

    The index row, plus the two facts a reader cannot check without them. A
    page claims to carry results under one code in a stated order; without the
    timestamp nothing on the other side can tell whether the order is the one
    claimed, and without the classification nothing can tell whether a row
    belongs on the page at all. Both would be well-formed rows either way,
    which is exactly the failure worth catching: a result shown under a heading
    it has nothing to do with.
    """
    classification = entry.get("classification") or {}
    return {
        **summary,
        "published_at": published_at(entry),
        "classification": {
            "arxiv": list(classification.get("arxiv", [])),
            "msc2020": list(classification.get("msc2020", [])),
        },
    }


def front_row(entry: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    """The same row, plus what an item of this code's feed needs.

    `feeds/<kind>/<code>.xml` is a rendering of this page and reads nothing
    else. The abstract is carried here rather than left to the feed builder,
    because a feed builder that opened the records to find it would read fifty
    records per code per publication, and the whole reason this document exists
    is that a publication must not read the registry to describe what changed
    in it. It is on the front page only: fifty abstracts is a few tens of
    kilobytes, and repeating them down the archive pages would be the size of
    the registry again for a field nothing there renders.
    """
    return {**archive_row(entry, summary), "abstract": entry.get("abstract", "")}


def build_subjects(
    output: pathlib.Path,
    entries: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    every: list[dict[str, Any]] | None = None,
) -> list[pathlib.Path]:
    """A front page and an archive for every code the registry has ever used.

    `entries` is every active version and `summaries` the index rows for them,
    in the same order. `every` is every version the database holds, withdrawn
    ones included, and is only ever read for which codes and which pages have
    to exist. A code is seeded from all of them and filled from the current
    ones, so a code whose only classifier was superseded or withdrawn keeps
    answering with an empty page rather than starting to 404 at a URL someone
    has linked. That is a handful of small objects per code, bounded by the
    classification vocabulary rather than by the registry.
    """
    rows = {
        (str(summary["id"]), int(summary["version"])): summary
        for summary in summaries
    }
    written: list[pathlib.Path] = []
    for kind in ("arxiv", "msc"):
        (output / "subjects" / kind).mkdir(parents=True, exist_ok=True)
    everything = entries if every is None else every
    seeds = code_seeds(everything)
    for (kind, code), selected in sorted(by_code(everything, latest_entries(entries)).items()):
        written.extend(
            write_code(
                output,
                kind,
                code,
                [
                    (entry, rows[(str(entry["id"]), int(entry["version"]))])
                    for entry in selected
                ],
                sorted(seeds[(kind, code)]),
            )
        )
    return written


def write_code(
    output: pathlib.Path,
    kind: str,
    code: str,
    selected: list[tuple[dict[str, Any], dict[str, Any]]],
    seeds: list[str],
) -> list[pathlib.Path]:
    """One code's front page and archive, from its current versions.

    `selected` is newest first, which `selection` decides, so the front page is
    a prefix of it and the archive is the same rows put back into registration
    order by the page they belong on. `seeds` is every result ever classified
    under the code, so that a page whose last row was superseded away is served
    empty rather than disappearing from under a reader.
    """
    return day_pages.write_collection(
        output,
        head_path=head_path(kind, code),
        directory=directory(kind, code),
        rows=[archive_row(entry, summary) for entry, summary in selected],
        seeds=seeds,
        extra={
            "kind": kind,
            "code": code,
            "entries": [
                front_row(entry, summary)
                for entry, summary in selected[:SUBJECT_PAGE_ITEMS]
            ],
        },
    )
