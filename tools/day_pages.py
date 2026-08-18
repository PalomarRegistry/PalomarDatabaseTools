#!/usr/bin/env python3
"""A collection of rows paged by the day the result entered the registry.

Browsing the whole registry and browsing one classification code want the same
thing: every row, in registration order, in pages small enough that adding one
result rewrites one of them. A hundred fixed shards did that for browsing and
did not: a shard carries a hundredth of the registry, so a publication still
generated and uploaded O(S/100) bytes, and the registry still paid O(S²) for
browsing over its life. A divisor is not a bound.

The page a row belongs on is read from the identifier and from nothing else.
`PALOMAR-YYYY-MM-DD-NNNNNN` carries the day the result entered the registry and
a serial that counts from 1 within that day, so the day, and
`(serial - 1) // PAGE_SERIALS + 1` within it, are a pure function of the
identifier. Nothing has to be consulted to find the page a row is on, no row
ever moves from one page to another, and a page is written only when something
in it changes. That day is when the submitter's consent was acted on and not
when their automated review completed, which is PalomarReviewer's outcome and
argued there. A write can still land on a day already past, because a new
version keeps its first version's identifier and a withdrawal takes a row off
that same page; it rewrites the one page it lands on, which is what every other
write here costs too. A directory mapping rows to pages would have answered the
same question and is exactly what this must not have: it is O(pages) rewritten
on every publication, which is the same quadratic with a bigger divisor.

Above the pages are two levels, and neither of them is a list of pages. The
days of one year are at most 366 rows whatever the registry holds. The head
names years, which arrive at one row a year -- the one thing here that grows
with time rather than with the change, and a few hundred bytes a decade. A
flat list of days would have been neither: at a bounded number of submissions a
day, the number of days grows with the registry, so a document naming all of
them is O(S) again.

Every version of a result shares its result's page, because the page is read
from the identifier and a new version reuses the identifier. So a page holds at
most `PAGE_SERIALS` results, and at most `PAGE_SERIALS` times the cap on
versions per result rows. There is no separate row cap to trip: a cap that
could refuse a page would have no remedy, since a day cannot be repaged after
the fact without moving rows that readers have already been pointed at.

Not naming the level below leaves a reader who has one of these documents and
nothing else unable to reach the next: the head named years and no path, so
getting from it to a page meant knowing the URL grammar this module happens to
use, which no served document stated. Each document above the pages therefore
carries one template naming its children -- `year_path` on the head,
`page_path` on a year -- and the pages carry a `path` per row already. One
string, whatever the collection holds, so the bound above is untouched: a
document naming its children individually is the O(pages) directory this
layout exists to avoid, and a template is not one.

The templates are written by the same functions that write the real paths,
with a placeholder in place of the value, so a template that disagreed with
where the collection actually put a document would have to be a disagreement
between one call and another of one function.
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any, Iterable

SCHEMA_VERSION = 2
# Two hundred results a page. A result carries a handful of versions, so a page
# is a few tens of kilobytes of index rows, and a day that registers more than
# two hundred results simply opens another page.
PAGE_SERIALS = 200
ID_RE = re.compile(r"PALOMAR-(([0-9]{4})-[0-9]{2}-[0-9]{2})-([0-9]{6})")


def coordinate(identifier: str) -> tuple[str, str, int]:
    """The year, day and page one result is browsed on, for its whole life.

    Refused rather than guessed at for an identifier of any other shape. A
    guessed page puts a record where no reader will look for it, and the record
    is then missing from browsing while every other surface still has it.
    """
    match = ID_RE.fullmatch(identifier)
    if match is None:
        raise ValueError(f"cannot page an identifier of this shape: {identifier!r}")
    day, year, serial = match.group(1), match.group(2), int(match.group(3))
    return year, day, (serial - 1) // PAGE_SERIALS + 1


def page_path(directory: str, day: str, page: int | str) -> str:
    return f"{directory}/{day}/{page}.json"


def year_path(directory: str, year: str) -> str:
    return f"{directory}/{year}.json"


# RFC 6570 level-1 expressions, expanded from the row a reader already has:
# `{year}` from a head row, `{day}` from a year row, and `{page}` from any
# integer in that row's closed `first_page`..`last_page` interval.
def year_template(directory: str) -> str:
    return year_path(directory, "{year}")


def page_template(directory: str) -> str:
    return page_path(directory, "{day}", "{page}")


def _write(output: pathlib.Path, relative: str, document: dict[str, Any]) -> pathlib.Path:
    target = output / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return target


def sort_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Registration order, which is identifier order and then version order.

    Serials count from 1 within a date and dates never go backwards, so sorting
    the identifiers as strings is the order the results were registered in.
    That is why no row carries an ordinal: an ordinal recorded beside an
    identifier is one more thing that can disagree with it, and nothing
    downstream could tell which of the two was right.
    """
    return sorted(rows, key=lambda row: (str(row["id"]), int(row["version"])))


def group(
    rows: Iterable[dict[str, Any]],
    seeds: Iterable[str] = (),
) -> dict[tuple[str, str, int], list[dict[str, Any]]]:
    """Every row, filed under the page its identifier puts it on.

    `seeds` are identifiers that must have a page whether or not they have a
    row on it. A page is seeded by every version the registry has ever had and
    filled by the ones being served, so a page that once held a row and no
    longer does is served empty rather than disappearing. That is a different
    answer from "no such page", and it is the one a reader can act on -- and it
    is what lets a release that patches a page agree, byte for byte, with one
    that rebuilds the whole collection, which it could not if the rebuild
    simply stopped writing a page the patch had emptied.
    """
    pages: dict[tuple[str, str, int], list[dict[str, Any]]] = {
        coordinate(identifier): [] for identifier in seeds
    }
    for row in rows:
        pages.setdefault(coordinate(str(row["id"])), []).append(row)
    return {key: sort_rows(value) for key, value in pages.items()}


def page_document(day: str, page: int, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "day": day,
        "page": page,
        "entries": sort_rows(rows),
    }


def day_row(day: str, pages: dict[int, list[dict[str, Any]]]) -> dict[str, Any]:
    """What a year document says about one day.

    The current registration contract allocates every day contiguously from
    serial 1, so `first_page` is 1. It remains explicit beside `last_page` so a
    day row states the exact closed interval a reader traverses.
    """
    numbers = sorted(pages)
    rows = [row for number in numbers for row in pages[number]]
    return {
        "day": day,
        "first_page": numbers[0],
        "last_page": numbers[-1],
        "results": len({str(row["id"]) for row in rows}),
        "versions": len(rows),
    }


def year_row(year: str, days: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "year": year,
        "days": len(days),
        "results": sum(int(day["results"]) for day in days),
        "versions": sum(int(day["versions"]) for day in days),
    }


def year_document(directory: str, year: str, days: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "year": year,
        "page_path": page_template(directory),
        "days": sorted(days, key=lambda row: str(row["day"])),
    }


def head_document(
    directory: str,
    years: list[dict[str, Any]],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ordered = sorted(years, key=lambda row: str(row["year"]))
    return {
        "schema_version": SCHEMA_VERSION,
        **(extra or {}),
        "results": sum(int(year["results"]) for year in ordered),
        "versions": sum(int(year["versions"]) for year in ordered),
        "year_path": year_template(directory),
        "years": ordered,
    }


def write_collection(
    output: pathlib.Path,
    *,
    head_path: str,
    directory: str,
    rows: Iterable[dict[str, Any]],
    seeds: Iterable[str] = (),
    extra: dict[str, Any] | None = None,
) -> list[pathlib.Path]:
    """Write every page, every year document, and the head.

    A full rebuild only. An incremental release writes the pages it touched and
    patches the two documents above them, which is what stops a publication
    generating the whole registry to add one row to it.
    """
    pages = group(rows, seeds)
    written: list[pathlib.Path] = []
    by_year: dict[str, dict[str, dict[int, list[dict[str, Any]]]]] = {}
    for (year, day, page), page_rows in sorted(pages.items()):
        by_year.setdefault(year, {}).setdefault(day, {})[page] = page_rows
        written.append(
            _write(output, page_path(directory, day, page), page_document(day, page, page_rows))
        )
    years: list[dict[str, Any]] = []
    for year, days in sorted(by_year.items()):
        day_rows = [day_row(day, day_pages) for day, day_pages in sorted(days.items())]
        years.append(year_row(year, day_rows))
        written.append(
            _write(output, year_path(directory, year), year_document(directory, year, day_rows))
        )
    written.append(_write(output, head_path, head_document(directory, years, extra)))
    return written
