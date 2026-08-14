"""The one rule every ordering surface reads.

`recent.json`, the feeds and the subject pages all ask when a version became
news. Three answers to that would agree until one of them changed, and the
disagreement would show as a landing page, a feed and a subject page naming
different newest results with none of them wrong on its own terms. So the rule
is here once, and this is what it says.
"""

from __future__ import annotations

import datetime as dt

import pytest
import selection


def _entry(**overrides):
    entry = {
        "id": "PALOMAR-2026-07-29-000001",
        "version": 1,
        "accepted_at": "2026-07-29",
        "registered_at": "2026-07-29T09:14:07Z",
        "review": {"reviewed_at": "2026-07-28T12:57:32Z"},
    }
    entry.update(overrides)
    return entry


def test_a_version_is_ordered_by_the_moment_it_was_registered():
    assert selection.published_at(_entry()) == "2026-07-29T09:14:07Z"


def test_the_review_date_no_longer_decides_anything_here():
    """It used to, and the two are days apart by design: nothing is registered
    until the submitter has read their review and consented. A record whose
    review is older than another's can be the newer registration, so a surface
    reading the review orders the two the wrong way round."""
    reviewed_much_later = _entry(review={"reviewed_at": "2030-01-01T00:00:00Z"})

    assert selection.published_at(reviewed_much_later) == "2026-07-29T09:14:07Z"


def test_a_record_with_no_registration_instant_cannot_be_ordered_at_all():
    """Refused rather than guessed at. A fallback is what let this module
    return two different kinds of answer, and a page ordered partly on one and
    partly on the other is in no order anybody can describe."""
    entry = _entry()
    del entry["registered_at"]

    with pytest.raises(KeyError):
        selection.published_at(entry)


def test_a_date_where_an_instant_belongs_is_refused():
    """A date parsed as an instant is midnight, so a row carrying one would
    sort ahead of every row registered that day and nothing would say so."""
    assert selection.parsed("2026-07-29T09:14:07Z") == dt.datetime(
        2026, 7, 29, 9, 14, 7, tzinfo=dt.UTC
    )

    with pytest.raises(ValueError, match="RFC3339 instant"):
        selection.parsed("2026-07-29")


def test_rows_and_records_are_put_in_the_same_order():
    """A release that patches a page has rows; a rebuild has records. Two
    orderings would agree until one of them changed, and the disagreement would
    show as a page whose order depends on whether the release that last touched
    it was incremental."""
    older = _entry(id="PALOMAR-2026-07-29-000001", registered_at="2026-07-29T09:00:00Z")
    newer = _entry(id="PALOMAR-2026-07-30-000001", registered_at="2026-07-30T09:00:00Z")
    rows = [
        {"id": entry["id"], "published_at": selection.published_at(entry)}
        for entry in (older, newer)
    ]

    assert [entry["id"] for entry in selection.latest_entries([older, newer])] == [
        row["id"] for row in selection.rows_newest_first(rows)
    ]
