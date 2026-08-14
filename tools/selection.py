"""Which records a derived surface shows, and in what order.

The category feeds and the subject pages answer the same question about the
same records: the current versions carrying one classification code, newest
first. Two implementations of that would agree until one of them learned which
of a record's several dates decides the order, and the disagreement would show
as a feed and a page naming different results under the same heading, with
neither of them wrong on its own terms.

So the rule lives here once, and both read it.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

ARXIV_RE = re.compile(r"[a-z]+(?:-[a-z]+)*(?:\.[A-Za-z-]+)?")
MSC_RE = re.compile(r"[0-9]{2}(?:[A-Z][0-9]{2}|-[0-9]{2})")
# The one shape `registered_at` takes, and therefore the one shape a row's
# `published_at` takes: the schema's own timestamp pattern.
TIMESTAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


def published_at(entry: dict[str, Any]) -> str:
    """When a version is treated as new, which is when it was registered.

    One timestamp, and it is the moment the submitter's consent was acted on.
    Browsing pages a row by the day in its identifier, which is that same
    moment, so the surface that lists everything and the surfaces that order it
    now answer from one fact and cannot disagree.

    Not `accepted_at`. That is a date rather than an instant, so it cannot
    order two results registered on one day, and every later version inherits
    it, so ordering by it would put a v2 registered years later among its
    siblings from the year of the v1.

    Not `review.reviewed_at` either, which is what this returned until now. A
    review's verdict and the registration it leads to are different moments,
    and since the identifier stopped taking the review's date they can be days
    apart: nothing is registered until the submitter has read their review and
    consented, and they may take as long as they like. At a couple of hundred
    registrations a day that gap is a newly registered result ordered behind
    two hundred older ones and so absent from `recent.json` altogether --
    registered, and invisible on the landing page.
    """
    return str(entry["registered_at"])


def published_datetime(entry: dict[str, Any]) -> dt.datetime:
    return parsed(published_at(entry))


def parsed(value: str) -> dt.datetime:
    """One instant, refused rather than guessed at.

    A date was accepted here while `published_at` could fall back to
    `accepted_at`. Keeping that tolerance would let a row carrying a date sort
    against rows carrying instants as though it had been registered at
    midnight, which is an order no reader could explain and nothing else would
    report.
    """
    if not TIMESTAMP_RE.fullmatch(value):
        raise ValueError(f"not an RFC3339 instant in Z: {value!r}")
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def order_key(published_at_value: str, identifier: str) -> tuple[dt.datetime, str]:
    """Newest first, read from a row rather than from a record.

    A release that patches a page has the parent's rows and the records it
    added, and nothing else, so it has to order rows. A rebuild has records. If
    those were two orderings they would agree until one of them was changed,
    and the disagreement would show as a page whose order depends on whether
    the release that last touched it was incremental -- which nothing else in
    the system could detect and no reader could explain.
    """
    return (parsed(published_at_value), identifier)


def latest_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The current version of each result, newest first."""
    latest: dict[str, dict[str, Any]] = {}
    for entry in entries:
        previous = latest.get(entry["id"])
        if previous is None or entry["version"] > previous["version"]:
            latest[entry["id"]] = entry
    return sorted(
        latest.values(),
        key=lambda entry: order_key(published_at(entry), str(entry["id"])),
        reverse=True,
    )


def rows_newest_first(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The same order over rows a page already carries."""
    return sorted(
        rows,
        key=lambda row: order_key(str(row["published_at"]), str(row["id"])),
        reverse=True,
    )


def codes_of(entry: dict[str, Any]) -> list[tuple[str, str]]:
    """The classification codes one version carries, checked as it is read.

    A code becomes a filename and a URL, so a code that is not a code is a way
    out of the directory it was supposed to be written into. Checked here
    because this is the one place every surface reads them from.
    """
    classification = entry.get("classification") or {}
    found: list[tuple[str, str]] = []
    for code in classification.get("arxiv", []):
        if not isinstance(code, str) or not ARXIV_RE.fullmatch(code):
            raise ValueError(f"invalid arXiv category in registry entry: {code!r}")
        found.append(("arxiv", code))
    for code in classification.get("msc2020", []):
        if not isinstance(code, str) or not MSC_RE.fullmatch(code):
            raise ValueError(f"invalid MSC2020 category in registry entry: {code!r}")
        found.append(("msc", code))
    return found


def code_seeds(every_version: list[dict[str, Any]]) -> dict[tuple[str, str], set[str]]:
    """Which results have ever been classified under each code.

    Seeding from every version rather than from the current ones is what keeps
    a code answering after its last classifier is superseded, instead of
    starting to 404 at a URL someone has subscribed to or linked. It is also
    what keeps that code's archive pages in place: a page that once held a row
    and stopped is served empty, which is a different answer from "no such
    page" and the one a reader can act on.
    """
    seeds: dict[tuple[str, str], set[str]] = {}
    for entry in every_version:
        for code in codes_of(entry):
            seeds.setdefault(code, set()).add(str(entry["id"]))
    return seeds


def by_code(
    every_version: list[dict[str, Any]],
    current: list[dict[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Every code the registry has ever used, against the current results in it.

    Seeded from every version and filled from the current ones. That costs a
    handful of small objects per code ever used, which is bounded by the
    classification vocabulary and not by the registry: MSC2020 has some
    thousands of codes however many results there are.
    """
    codes: dict[tuple[str, str], list[dict[str, Any]]] = {
        code: [] for code in code_seeds(every_version)
    }
    for entry in current:
        for code in codes_of(entry):
            codes[code].append(entry)
    return codes
