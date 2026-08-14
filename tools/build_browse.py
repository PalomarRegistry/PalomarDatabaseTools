#!/usr/bin/env python3
"""The registry as pages of the day each result entered it.

`index.json` names every active version, so adding one record rewrote all of
it. A hundred fixed shards replaced that and did not fix it: a shard carries a
hundredth of the registry, so a publication still generated and uploaded
O(S/100) bytes for browsing, once per accepted result, and O(S²) over the
registry's life. A divisor is not a bound.

A page here carries the results that entered the registry on one day whose
serials fall in one band of two hundred, at `browse/<YYYY-MM-DD>/<page>.json`.
Which page a row is on is read from the identifier alone, so no row ever moves
and any one write touches one page.

The day is the date the record carries as `accepted_at`, which is the day of
its version 1's `registered_at` and so the day the result entered the registry.
It is not the day its review reached a verdict.
Registration waits on the submitter consenting to their review and they may
take as long as they like over that, so dating a result by its review would let
anyone who wanted an earlier position simply hold their consent. PalomarReviewer
decides the date and holds the argument; nothing here does.

A day that is past is still not sealed. A new version rewrites the page its
first version sits on however many years later, a withdrawal takes a row off
that page, and a registration retried across midnight lands on the previous
day's page under the identifier it had already reserved. All of that costs one
page rewritten, which is what an append to today costs, and it is the only
bound this layout has ever rested on.

The two documents above the pages are `browse/<YYYY>.json`, which is at most
366 rows whatever the registry holds, and `browse/index.json`, which names the
years. Neither is a list of pages, deliberately: a directory naming every page
is O(pages) rewritten per publication, which is the same quadratic the shards
had.

The serial is a position, not a hash: sequential allocation puts one day's
results in registration order, and paging on the whole serial gives every row
a stable bounded page without a directory lookup.
"""

from __future__ import annotations

import pathlib
from typing import Any

import day_pages
from day_pages import coordinate  # noqa: F401  (re-exported: the placement rule)

DIRECTORY = "browse"
HEAD_PATH = "browse/index.json"


def build_browse(
    output: pathlib.Path,
    summaries: list[dict[str, Any]],
    seeds: list[str] | None = None,
) -> list[pathlib.Path]:
    """Write every browse page, year document and the head.

    A full rebuild. Every page is written, and the publisher uploads only the
    ones whose bytes changed, so the untouched ones cost one hash each and no
    write; an incremental release does not build them at all.

    `summaries` are the versions being served and `seeds` the identifiers of
    every version the registry has, withdrawn ones included. A page seeded by a
    withdrawn record is served empty rather than vanishing, which is the same
    answer its tombstone gives: the record was here and is not being served.
    """
    return day_pages.write_collection(
        output,
        head_path=HEAD_PATH,
        directory=DIRECTORY,
        rows=summaries,
        seeds=[str(row["id"]) for row in summaries] if seeds is None else seeds,
    )
