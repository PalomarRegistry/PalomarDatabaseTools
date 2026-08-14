"""A run that cannot ask must not look like one that asked and got good news.

The check re-asked GitHub about every preservation row every six hours. At
roughly seventeen rows per entry, each asked about twice, against a budget of a
thousand requests an hour, that stopped fitting at about thirty entries — and
the response to running out was to write "unknown" everywhere and exit zero.
"""

from __future__ import annotations

import json
import urllib.error

import pytest
from check_source_availability import (
    CAPACITY_EXIT,
    INCOMPLETE_EXIT,
    GitHubCommitChecker,
    build_manifest,
)


def _rate_limited(code=403, remaining="0"):
    def opener(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://api.github.com/x", code, "", {"x-ratelimit-remaining": remaining}, None
        )

    return opener


def test_a_refused_request_is_not_a_failed_one(monkeypatch):
    """Retrying a rate limit spends more of the budget that was just exhausted,
    and the reset can be an hour away."""
    calls = []

    def opener(*args, **kwargs):
        calls.append(args)
        return _rate_limited()(*args, **kwargs)

    monkeypatch.setattr("urllib.request.urlopen", opener)
    checker = GitHubCommitChecker("token")
    status, detail = checker("example/one", "c" * 40)

    assert status == "budget"
    assert "budget" in detail
    assert len(calls) == 1, "a refusal must not be retried"
    assert checker.budget_exhausted


def test_once_refused_the_run_stops_asking(monkeypatch):
    calls = []

    def opener(*args, **kwargs):
        calls.append(args)
        return _rate_limited()(*args, **kwargs)

    monkeypatch.setattr("urllib.request.urlopen", opener)
    checker = GitHubCommitChecker("token")
    for index in range(5):
        assert checker(f"example/{index}", "c" * 40)[0] == "budget"
    assert len(calls) == 1, "every later query would answer unknown too"


def test_the_request_that_discovers_exhaustion_counts_as_attempted(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _rate_limited())
    checker = GitHubCommitChecker("token")

    assert checker("example/one", "c" * 40)[0] == "budget"
    assert checker("example/two", "d" * 40)[0] == "budget"
    assert checker.provider_queries_attempted == 1


def test_secondary_rate_limit_is_also_fail_closed(monkeypatch):
    def secondary(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://api.github.com/x", 403, "", {"retry-after": "60"}, None
        )

    monkeypatch.setattr("urllib.request.urlopen", secondary)
    checker = GitHubCommitChecker("token")
    assert checker("example/one", "c" * 40)[0] == "budget"
    assert checker.budget_exhausted


def test_an_ordinary_failure_is_still_retried(monkeypatch):
    calls = []

    def opener(*args, **kwargs):
        calls.append(args)
        raise urllib.error.HTTPError("https://api.github.com/x", 500, "", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", opener)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    assert GitHubCommitChecker("token")("example/one", "c" * 40)[0] == "transient"
    assert len(calls) == 3


def test_a_run_asks_about_a_bounded_number_of_rows(db, tmp_path):
    """The property that makes this stop growing with the registry."""
    db.add_entry("PALOMAR-2026-07-29-000002", 1)
    asked = []

    def checker(repository, commit):
        asked.append((repository, commit))
        return "available", None

    manifest = build_manifest(
        db.path, {}, checker,
        generated_at="2026-08-07T00:00:00Z", database_commit="d" * 40, max_queries=2,
    )
    assert len(asked) == 2
    coverage = manifest["coverage"]
    assert coverage["queries_selected"] == coverage["queries_attempted"] == 2
    assert coverage["queries_skipped"] == coverage["queries_total"] - 2
    assert coverage["query_budget"] == 2


def test_a_row_it_did_not_ask_about_keeps_what_was_known(db):
    """Not attempted is not the same as attempted and unknown."""
    db.add_entry("PALOMAR-2026-07-29-000002", 1)

    def checker(repository, commit):
        return "available", None

    full = build_manifest(
        db.path, {}, checker,
        generated_at="2026-08-07T00:00:00Z", database_commit="d" * 40,
    )
    carried = build_manifest(
        db.path, full, checker,
        generated_at="2026-08-07T12:00:00Z", database_commit="d" * 40, max_queries=0,
    )
    for row in carried["repositories"]:
        assert row["original"]["status"] == "available"
        assert row["original"]["checked_at"] == "2026-08-07T00:00:00Z", (
            "a row nobody asked about must not claim a fresh observation"
        )
    assert carried["coverage"]["queries_attempted"] == 0


def test_the_manifest_says_what_it_established(db):
    db.add_entry("PALOMAR-2026-07-29-000002", 1)

    def checker(repository, commit):
        return "available", None

    manifest = build_manifest(
        db.path, {}, checker,
        generated_at="2026-08-07T00:00:00Z", database_commit="d" * 40,
    )
    coverage = manifest["coverage"]
    assert coverage["queries_answered"] == coverage["queries_attempted"] > 0
    assert coverage["budget_exhausted"] is False
    assert coverage["oldest_observation_at"] == "2026-08-07T00:00:00Z"
    assert coverage["oldest_observation_age_seconds"] == 0
    assert coverage["observations_fresh"] == coverage["observations_total"] > 0


def test_a_blind_run_reports_it_rather_than_succeeding(db, tmp_path, monkeypatch):
    """This is the failure the whole change exists for: the manifest looked
    freshly generated, every status said unknown, and the run exited zero."""
    import check_source_availability as availability

    class Blind:
        budget_exhausted = True

        def __call__(self, repository, commit):
            return "budget", "HTTP 403, request budget exhausted"

    monkeypatch.setattr(availability, "GitHubCommitChecker", lambda _token: Blind())
    monkeypatch.setattr(
        availability.subprocess, "run",
        lambda *_a, **_k: type("R", (), {"stdout": "d" * 40 + "\n"})(),
    )
    output = tmp_path / "availability.json"
    code = availability.main(["--root", str(db.path), "--output", str(output)])
    assert code == INCOMPLETE_EXIT, "a run that knows nothing must not exit zero"
    coverage = json.loads(output.read_text())["coverage"]
    assert coverage["budget_exhausted"] is True
    assert coverage["queries_attempted"] == 0
    assert coverage["queries_skipped"] == coverage["queries_total"]


def test_main_reports_an_impossible_refresh_cycle(db, tmp_path, monkeypatch):
    import check_source_availability as availability

    rows = [
        {
            "source_repository": f"source/{index:04d}",
            "commit": f"{index:040x}",
            "fork_repository": f"archive/{index:04d}",
        }
        for index in range(1_201)
    ]
    monkeypatch.setattr(availability, "preservation_rows", lambda _root: rows)
    monkeypatch.setattr(
        availability.subprocess, "run",
        lambda *_a, **_k: type("R", (), {"stdout": "d" * 40 + "\n"})(),
    )
    monkeypatch.setattr(
        availability,
        "GitHubCommitChecker",
        lambda _token: lambda _repository, _commit: ("available", None),
    )

    output = tmp_path / "availability.json"
    assert availability.main(["--root", str(db.path), "--output", str(output)]) == CAPACITY_EXIT
    coverage = json.loads(output.read_text())["coverage"]
    assert coverage["queries_total"] == 2_402
    assert coverage["planned_refresh_cycle_hours"] == 24
