#!/usr/bin/env python3
"""Produce the touched pages of a release by patching the ones being served.

Staging used to build every derived surface from the whole active set on every
publication: the browse pages, a page per classification code, the version
index of every result, and the feeds, all of it generated and hashed once per
accepted result. Nothing was uploaded that had not changed, but everything was
built, so a publication still cost the size of the registry and the registry
still paid O(S²) over its life.

A release touches a handful of results. Each of those appears on one browse
page, on the pages of a handful of codes, on the recent page, and in its own
version index; every other page of every other day and code is a document this
release does not change. So the release fetches exactly those pages from what
is being served, applies the difference, and writes them back. Nothing else is
read, built or hashed, and the delta names only what was written.

Two things cannot be patched safely. A page that has to *refill* -- the recent
page or a code's front page -- needs a row from below which this release does
not hold. And a prior document outside the current closed shape cannot be used
as evidence for an incremental update: guessing at its missing structure can
silently discard untouched rows. Both request `Rebuild`. Falling back is
always correct and merely slow, which is the rule everywhere here.

A prior document that is not there is treated as one that was never published,
not as an error. A code's front page exists as soon as any served result
carries the code, so its absence means no served result does. An object that
went missing some other way is drift, which no incremental release could see
and which `whole-database-sweep.yml` is what looks for. The two documents that
must exist once anything has been published -- the recent page and the browse
head -- are checked against the parent's record count, because for those two
"never published" and "gone" are distinguishable and worth distinguishing.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Iterable

import day_pages
from build_browse import DIRECTORY as BROWSE_DIRECTORY
from build_browse import HEAD_PATH as BROWSE_HEAD
from build_recent import RECENT_ITEMS, RECENT_PATH, RECENT_RENDERS_PATH
from build_recent import render_hash, render_page
from build_recent import row as recent_row
from build_recent import validate_recent, validate_recent_renders
from build_recent import write_recent, write_recent_renders
from build_subjects import SUBJECT_PAGE_ITEMS, archive_row, directory, front_row, head_path
from selection import codes_of, parsed, rows_newest_first


class Rebuild(Exception):
    """Raised when patching cannot produce what a rebuild would produce."""


INDEX_ROW_KEYS = {"id", "version", "title", "status", "path"}
ARCHIVE_ROW_KEYS = INDEX_ROW_KEYS | {"published_at", "classification"}
FRONT_ROW_KEYS = ARCHIVE_ROW_KEYS | {"abstract"}
YEAR_ROW_KEYS = {"year", "days", "results", "versions"}
DAY_ROW_KEYS = {"day", "first_page", "last_page", "results", "versions"}


class Touched:
    """One result this release changed, and every version of it in the database.

    `every` comes from the canonical per-result projection rather than a
    published document. Its rows carry the historical presentation fields
    needed to rebuild `versions/<id>.json` and to tell which codes the result
    is leaving as well as which it is joining. The arriving current version is
    replaced with its complete immutable entry before rich recent/feed rows are
    built.
    """

    def __init__(
        self,
        identifier: str,
        every: list[tuple[dict[str, Any], dict[str, Any]]],
        active: list[tuple[dict[str, Any], dict[str, Any]]],
    ) -> None:
        self.id = identifier
        self.every = every
        self.active = active
        self.prior_active: list[tuple[dict[str, Any], dict[str, Any]]] | None = None

    @property
    def current(self) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """The version being served, which is the newest active one."""
        return max(self.active, key=lambda pair: int(pair[0]["version"]), default=None)

    def codes(self) -> set[tuple[str, str]]:
        """Every code any version of this result has ever carried.

        The union rather than the current version's, because a v2 that drops a
        code has to take its row out of that code's pages, and the only record
        that says which code that was is the version that carried it.
        """
        return {code for _summary, entry in self.every for code in codes_of(entry)}


def _invalid(relative: str, reason: str) -> Rebuild:
    return Rebuild(f"the release being served has an invalid {relative}: {reason}")


def _read(prior: pathlib.Path, relative: str) -> dict[str, Any] | None:
    path = prior / relative
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as reason:
        raise _invalid(relative, str(reason)) from reason
    if not isinstance(document, dict):
        raise _invalid(relative, "document is not an object")
    return document


def _exact(document: dict[str, Any], relative: str, keys: set[str]) -> None:
    if set(document) != keys:
        raise _invalid(relative, "document has the wrong fields")
    if (
        type(document["schema_version"]) is not int
        or document["schema_version"] != day_pages.SCHEMA_VERSION
    ):
        raise _invalid(relative, "schema_version is not the current version")


def _integer(value: object, relative: str, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise _invalid(relative, f"{field} is not an integer of at least {minimum}")
    return value


def _text(value: object, relative: str, field: str) -> str:
    if not isinstance(value, str):
        raise _invalid(relative, f"{field} is not a string")
    return value


def _rows(
    document: dict[str, Any], relative: str, field: str
) -> list[dict[str, Any]]:
    if field not in document:
        raise _invalid(relative, f"{field} is missing")
    values = document[field]
    if not isinstance(values, list) or not all(isinstance(row, dict) for row in values):
        raise _invalid(relative, f"{field} is not an array of objects")
    return values


def _index_row(
    row: dict[str, Any], relative: str, field: str, expected: set[str]
) -> dict[str, Any]:
    if set(row) != expected:
        raise _invalid(relative, f"{field} has the wrong fields")
    identifier = _text(row["id"], relative, f"{field}.id")
    version = _integer(row["version"], relative, f"{field}.version", minimum=1)
    for name in ("title", "status", "path"):
        _text(row[name], relative, f"{field}.{name}")
    if row["path"] != f"entries/{identifier}-v{version}.json":
        raise _invalid(relative, f"{field}.path does not name its entry")
    if expected != INDEX_ROW_KEYS:
        stamp = _text(row["published_at"], relative, f"{field}.published_at")
        try:
            parsed(stamp)
        except ValueError as error:
            raise _invalid(relative, f"{field}.published_at: {error}") from error
        classification = row["classification"]
        if not isinstance(classification, dict) or set(classification) != {
            "arxiv", "msc2020"
        }:
            raise _invalid(relative, f"{field}.classification has the wrong fields")
        for kind in ("arxiv", "msc2020"):
            codes = classification[kind]
            if not isinstance(codes, list) or not all(isinstance(code, str) for code in codes):
                raise _invalid(relative, f"{field}.classification.{kind} is not strings")
    if expected == FRONT_ROW_KEYS:
        _text(row["abstract"], relative, f"{field}.abstract")
    return dict(row)


def _head_years(
    document: dict[str, Any],
    relative: str,
    extra: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    extra_keys = set(extra or {})
    # A served head from before the templates were published has the wrong
    # fields here and so asks for a rebuild, which is what the transition
    # wants: patching would have given the templates to the documents this
    # release happened to touch and left every other one without them, and a
    # tree whose shape depends on which day was written last is not a contract.
    _exact(
        document,
        relative,
        {"schema_version", "results", "versions", "year_path", "years"} | extra_keys,
    )
    _integer(document["results"], relative, "results")
    _integer(document["versions"], relative, "versions")
    if extra is not None:
        for name in ("kind", "code"):
            if document[name] != extra[name]:
                raise _invalid(relative, f"{name} does not match its path")
        for position, row in enumerate(_rows(document, relative, "entries")):
            _index_row(row, relative, f"entries[{position}]", FRONT_ROW_KEYS)
    years: dict[str, dict[str, Any]] = {}
    for position, row in enumerate(_rows(document, relative, "years")):
        field = f"years[{position}]"
        if set(row) != YEAR_ROW_KEYS:
            raise _invalid(relative, f"{field} has the wrong fields")
        year = _text(row["year"], relative, f"{field}.year")
        for name in ("days", "results", "versions"):
            _integer(row[name], relative, f"{field}.{name}")
        if year in years:
            raise _invalid(relative, f"{field}.year is duplicated")
        years[year] = dict(row)
    return years


def _year_days(
    document: dict[str, Any], relative: str, year: str
) -> dict[str, dict[str, Any]]:
    _exact(document, relative, {"schema_version", "year", "page_path", "days"})
    if document["year"] != year:
        raise _invalid(relative, "year does not match its path")
    days: dict[str, dict[str, Any]] = {}
    for position, row in enumerate(_rows(document, relative, "days")):
        field = f"days[{position}]"
        if set(row) != DAY_ROW_KEYS:
            raise _invalid(relative, f"{field} has the wrong fields")
        day = _text(row["day"], relative, f"{field}.day")
        first = _integer(row["first_page"], relative, f"{field}.first_page", minimum=1)
        _integer(row["last_page"], relative, f"{field}.last_page", minimum=first)
        for name in ("results", "versions"):
            _integer(row[name], relative, f"{field}.{name}")
        if day in days:
            raise _invalid(relative, f"{field}.day is duplicated")
        days[day] = dict(row)
    return days


def _page_entries(
    document: dict[str, Any],
    relative: str,
    day: str,
    number: int,
    *,
    subject: bool,
) -> list[dict[str, Any]]:
    _exact(document, relative, {"schema_version", "day", "page", "entries"})
    if (
        document["day"] != day
        or type(document["page"]) is not int
        or document["page"] != number
    ):
        raise _invalid(relative, "day or page does not match its path")
    expected = ARCHIVE_ROW_KEYS if subject else INDEX_ROW_KEYS
    return [
        _index_row(row, relative, f"entries[{position}]", expected)
        for position, row in enumerate(_rows(document, relative, "entries"))
    ]


def prior_paths(touched: Iterable[Touched]) -> list[str]:
    """Exactly the published documents this release has to read.

    Computed from the git diff and bounded per-result projections alone, so the
    step that works it out needs no bucket credentials and the step that has
    them does nothing but fetch the names it is given.
    """
    paths = {RECENT_PATH, RECENT_RENDERS_PATH, BROWSE_HEAD}
    for item in touched:
        year, day, page = day_pages.coordinate(item.id)
        paths.add(day_pages.year_path(BROWSE_DIRECTORY, year))
        paths.add(day_pages.page_path(BROWSE_DIRECTORY, day, page))
        for kind, code in item.codes():
            where = directory(kind, code)
            paths.add(head_path(kind, code))
            paths.add(day_pages.year_path(where, year))
            paths.add(day_pages.page_path(where, day, page))
    return sorted(paths)


def _write(output: pathlib.Path, relative: str, document: dict[str, Any]) -> None:
    target = output / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _counts(rows: list[dict[str, Any]]) -> tuple[int, int]:
    return len({str(row["id"]) for row in rows}), len(rows)


def _patch_collection(
    output: pathlib.Path,
    prior: pathlib.Path,
    *,
    head: str,
    where: str,
    changes: dict[str, list[dict[str, Any]]],
    head_extra: dict[str, Any] | None = None,
    baseline_changes: dict[str, list[dict[str, Any]]] | None = None,
) -> None:
    """Rewrite the pages a change touches, and the two documents above them.

    `changes` replaces every row for one identifier at once, which it can
    because every version of a result shares one page: the page is read from
    the identifier and a new version reuses the identifier. So a page is
    patched by dropping that identifier's rows and putting back the ones being
    served, with no need to know what was there before.

    Only the day rows are arithmetic. A year document names every day of its
    year and the head names every year, so both are recomputed outright from
    what they already carry; a day's totals span pages this release has not
    read, so they move by the difference the touched pages made.
    """
    document = _read(prior, head)
    all_years = _head_years(document, head, head_extra) if document is not None else {}
    coordinates = {identifier: day_pages.coordinate(identifier) for identifier in changes}
    identifiers_by_page: dict[tuple[str, str, int], list[str]] = {}
    for identifier, coordinate in coordinates.items():
        identifiers_by_page.setdefault(coordinate, []).append(identifier)

    # The head down, so that a document which is missing can be told apart from
    # one that was never written. Read the other way round, a year document
    # that had gone missing would be taken for an empty year, and the release
    # would write one back naming only the day it touched -- silently dropping
    # every other day of that year from the only place they are named.
    years: dict[str, dict[str, dict[str, Any]]] = {}
    for year in {year for year, _day, _page in coordinates.values()}:
        relative = day_pages.year_path(where, year)
        found = _read(prior, relative)
        if found is None and year in all_years:
            raise Rebuild(f"{relative} is named by {head} and is not there")
        years[year] = _year_days(found, relative, year) if found is not None else {}

    pages: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    before: dict[tuple[str, str, int], tuple[int, int]] = {}
    for identifier, rows in changes.items():
        key = coordinates[identifier]
        if key not in pages:
            year, day, number = key
            relative = day_pages.page_path(where, day, number)
            found = _read(prior, relative)
            known = years[year].get(day)
            if found is None and known and int(known["first_page"]) <= number <= int(known["last_page"]):
                raise Rebuild(f"{relative} is inside a published day and is not there")
            existing = (
                _page_entries(
                    found, relative, day, number, subject=head_extra is not None
                )
                if found is not None
                else []
            )
            if baseline_changes is not None:
                for baseline_identifier in identifiers_by_page[key]:
                    if baseline_identifier not in baseline_changes:
                        raise Rebuild(
                            f"{relative} has no authenticated baseline for "
                            f"{baseline_identifier}"
                        )
                    existing = [
                        row
                        for row in existing
                        if str(row["id"]) != baseline_identifier
                    ] + baseline_changes[baseline_identifier]
            pages[key] = existing
            before[key] = _counts(existing)
            if (
                known is not None
                and int(known["first_page"]) == number
                and int(known["last_page"]) == number
            ):
                # A cancelled publication can have replaced this sole day
                # page before its year document. Anchor both to the served
                # version indexes so retrying applies the transition once,
                # rather than treating the interrupted page as the parent.
                known["results"], known["versions"] = before[key]
        pages[key] = [
            row for row in pages[key] if str(row["id"]) != identifier
        ] + rows

    moved: dict[tuple[str, str], list[int]] = {}
    for (year, day, number), rows in sorted(pages.items()):
        _write(output, day_pages.page_path(where, day, number), day_pages.page_document(day, number, rows))
        results, versions = _counts(rows)
        was_results, was_versions = before[(year, day, number)]
        entry = moved.setdefault((year, day), [0, 0, number, number])
        entry[0] += results - was_results
        entry[1] += versions - was_versions
        entry[2] = min(entry[2], number)
        entry[3] = max(entry[3], number)

    for (year, day), (results, versions, first, last) in sorted(moved.items()):
        rows = years[year]
        row = rows.get(day, {"day": day, "first_page": first, "last_page": last,
                             "results": 0, "versions": 0})
        row["results"] += results
        row["versions"] += versions
        row["first_page"] = min(int(row["first_page"]), first)
        row["last_page"] = max(int(row["last_page"]), last)
        rows[day] = row

    for year in sorted({year for year, _day in moved}):
        days = list(years[year].values())
        _write(
            output, day_pages.year_path(where, year), day_pages.year_document(where, year, days)
        )
        all_years[year] = day_pages.year_row(year, days)
    _write(output, head, day_pages.head_document(where, list(all_years.values()), head_extra))


def _refilled(prior_rows: list[dict[str, Any]], rows: list[dict[str, Any]], cap: int) -> list[dict[str, Any]]:
    """The newest `cap` rows, or `Rebuild` if the answer is below the page.

    A page that was full and is now short has lost a row to something that
    superseded, withdrew or reclassified a result, and what should take its
    place is the next newest, which this release does not hold. A page that was
    already short held everything there was, so nothing can be below it.
    """
    page = rows_newest_first(rows)[:cap]
    if len(prior_rows) >= cap and len(page) < cap:
        raise Rebuild("a capped page lost a row and what refills it is not here")
    return page


def patch(
    output: pathlib.Path,
    prior: pathlib.Path,
    touched: list[Touched],
    *,
    parent_records: int,
) -> list[tuple[str, str]]:
    """Write every surface this release changes. Returns the codes it touched."""
    recent = _read(prior, RECENT_PATH)
    browse_head = _read(prior, BROWSE_HEAD)
    if parent_records and (recent is None or browse_head is None):
        # Both exist as soon as anything has been published, so for these two
        # "never published" and "gone" are distinguishable, and treating a
        # missing one as empty would publish a landing page that had forgotten
        # the registry.
        raise Rebuild("the release being served is missing a document every release writes")
    if recent is not None:
        try:
            validate_recent(recent)
        except ValueError as error:
            raise _invalid(RECENT_PATH, str(error)) from error
    # Not part of the guard above. Absence here is indistinguishable between
    # "gone" and "no release has written one yet", which is the state of every
    # release until the first one after this document was introduced. That case
    # wants the same answer as drift does, and gets it below: a row on the page
    # whose hash is not to hand requests a rebuild, and one rebuild settles it.
    prior_renders = _read(prior, RECENT_RENDERS_PATH)
    if prior_renders is not None:
        try:
            validate_recent_renders(prior_renders)
        except ValueError as error:
            raise _invalid(RECENT_RENDERS_PATH, str(error)) from error

    _patch_collection(
        output,
        prior,
        head=BROWSE_HEAD,
        where=BROWSE_DIRECTORY,
        changes={item.id: [summary for summary, _entry in item.active] for item in touched},
        baseline_changes={
            item.id: [summary for summary, _entry in item.prior_active]
            for item in touched
            if item.prior_active is not None
        },
    )

    by_code: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = {}
    fronts: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in touched:
        current = item.current
        carried = set(codes_of(current[1])) if current else set()
        for code in item.codes():
            rows = by_code.setdefault(code, {})
            fronts.setdefault(code, [])
            if code in carried:
                summary, entry = current
                rows[item.id] = [archive_row(entry, summary)]
                fronts[code].append(front_row(entry, summary))
            else:
                rows[item.id] = []
    for (kind, code), changes in sorted(by_code.items()):
        head = head_path(kind, code)
        document = _read(prior, head)
        was = (
            [
                _index_row(row, head, f"entries[{position}]", FRONT_ROW_KEYS)
                for position, row in enumerate(_rows(document, head, "entries"))
            ]
            if document is not None
            else []
        )
        page = _refilled(
            was,
            [row for row in was if str(row["id"]) not in changes]
            + fronts[(kind, code)],
            SUBJECT_PAGE_ITEMS,
        )
        _patch_collection(
            output,
            prior,
            head=head,
            where=directory(kind, code),
            changes=changes,
            head_extra={"kind": kind, "code": code, "entries": page},
        )

    changed = {item.id for item in touched}
    was = list((recent or {}).get("entries", []))
    rows = [row for row in was if str(row["id"]) not in changed]
    # The hash of a result this release does not touch is only in the document
    # being served; the hash of one it does touch is in the record it holds,
    # and supersedes any prior row for that result.
    hashes = {
        (str(row["id"]), int(row["version"])): str(row["artifact_tree_sha256"])
        for row in (prior_renders or {}).get("renders", [])
    }
    for item in touched:
        current = item.current
        if current is not None:
            _summary, entry = current
            rows.append(recent_row(entry, len(item.active)))
            hashes[(entry["id"], entry["version"])] = render_hash(entry)
    page = _refilled(was, rows, RECENT_ITEMS)
    try:
        renders = render_page(page, hashes)
    except ValueError as error:
        # A row survived onto the page and its hash is not here to carry over.
        # Decided before either document is written, so the two are written
        # together or not at all.
        raise Rebuild(str(error)) from error
    write_recent(output, page)
    write_recent_renders(output, renders)
    return sorted(by_code)
