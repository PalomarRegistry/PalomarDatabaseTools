"""Per-observation freshness and bounded global-manifest behavior."""

from __future__ import annotations

import datetime as dt

import check_source_availability as availability
import pytest
from check_source_availability import archive_is_degraded, build_manifest
from source_availability_contract import (
    MAX_CLOCK_SKEW_SECONDS,
    MAX_OBSERVATION_AGE_SECONDS,
    normalize_observation,
)

NOW = dt.datetime(2026, 8, 8, 12, tzinfo=dt.UTC)


def _stamp(offset_seconds=0):
    return (NOW + dt.timedelta(seconds=offset_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _state(checked_at):
    return {
        "status": "available",
        "checked_at": checked_at,
        "last_attempt_at": _stamp(),
        "consecutive_missing": 0,
        "last_error": None,
    }


@pytest.mark.parametrize(
    "checked_at,expected_status",
    [
        (_stamp(-MAX_OBSERVATION_AGE_SECONDS), "available"),
        (_stamp(-MAX_OBSERVATION_AGE_SECONDS - 1), "unknown"),
        (_stamp(MAX_CLOCK_SKEW_SECONDS), "available"),
        (_stamp(MAX_CLOCK_SKEW_SECONDS + 1), "unknown"),
        (None, "unknown"),
        ("not-a-timestamp", "unknown"),
    ],
)
def test_known_status_requires_a_current_valid_checked_at(checked_at, expected_status):
    normalized = normalize_observation(_state(checked_at), as_of=NOW, field="row.original")
    assert normalized["status"] == expected_status
    if checked_at == "not-a-timestamp":
        assert normalized["checked_at"] is None


def test_stale_carried_rows_become_unknown_even_when_the_manifest_is_new(db):
    db.add_entry("PALOMAR-2026-07-29-000002", 1)
    first = build_manifest(
        db.path,
        {},
        lambda _repository, _commit: ("available", None),
        generated_at=_stamp(-MAX_OBSERVATION_AGE_SECONDS - 1),
        database_commit="d" * 40,
    )

    carried = build_manifest(
        db.path,
        first,
        lambda _repository, _commit: ("available", None),
        generated_at=_stamp(),
        database_commit="d" * 40,
        max_queries=0,
    )

    states = [
        state
        for row in carried["repositories"]
        for state in (row["original"], row["archive"])
    ]
    assert all(state["status"] == "unknown" for state in states)
    assert carried["coverage"]["observations_stale"] == len(states)
    assert carried["coverage"]["observations_fresh"] == 0


def test_synthetic_backlog_reports_budget_coverage_and_cycle(monkeypatch, tmp_path):
    rows = [
        {
            "source_repository": f"source/{index:02d}",
            "commit": f"{index:040x}",
            "fork_repository": f"archive/{index:02d}",
        }
        for index in range(5)
    ]
    monkeypatch.setattr(availability, "preservation_rows", lambda _root: rows)

    manifest = build_manifest(
        tmp_path,
        {},
        lambda _repository, _commit: ("available", None),
        generated_at=_stamp(),
        database_commit="d" * 40,
        max_queries=3,
    )
    coverage = manifest["coverage"]
    assert coverage["queries_total"] == 10
    assert coverage["query_budget"] == coverage["queries_selected"] == 3
    assert coverage["queries_attempted"] == coverage["queries_answered"] == 3
    assert coverage["queries_skipped"] == 7
    assert coverage["planned_refresh_cycle_runs"] == 4
    assert coverage["planned_refresh_cycle_hours"] == 24
    assert coverage["observations_total"] == 10
    assert coverage["observations_fresh"] == 3
    assert coverage["observations_unknown"] == coverage["observations_unobserved"] == 7


def test_query_selection_deduplicates_endpoints_and_has_a_stable_tiebreak(
    monkeypatch, tmp_path
):
    rows = [
        {
            "source_repository": "b/shared",
            "commit": "c" * 40,
            "fork_repository": "c/archive",
        },
        {
            "source_repository": "a/original",
            "commit": "c" * 40,
            "fork_repository": "b/shared",
        },
    ]
    monkeypatch.setattr(availability, "preservation_rows", lambda _root: rows)
    asked = []

    manifest = build_manifest(
        tmp_path,
        {},
        lambda repository, commit: asked.append((repository, commit)) or ("available", None),
        generated_at=_stamp(),
        database_commit="d" * 40,
        max_queries=1,
    )

    assert asked == [("a/original", "c" * 40)]
    assert manifest["coverage"]["queries_total"] == 3


def test_provider_capacity_is_observed_without_an_extra_request(monkeypatch):
    class Response:
        status = 200
        headers = {"x-ratelimit-limit": "1000", "x-ratelimit-remaining": "731"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response())
    checker = availability.GitHubCommitChecker("token")

    assert checker("example/project", "c" * 40) == ("available", None)
    assert checker.provider_query_limit == 1000
    assert checker.provider_queries_remaining == 731


def test_withdrawn_records_do_not_consume_the_refresh_queue(db):
    identifier = "PALOMAR-2026-07-29-000002"
    entry = db.entry_data(identifier, 1)
    entry["source"].update({
        "repository": "example/withdrawn-source",
        "repository_url": "https://github.com/example/withdrawn-source",
        "commit": "a" * 40,
        "tree_url": "https://github.com/example/withdrawn-source/tree/" + "a" * 40,
    })
    db.install_entry(entry)
    db.write_json("takedowns.json", {
        "schema_version": 1,
        "takedowns": [{
            "id": identifier,
            "version": 1,
            "taken_down_at": "2026-08-08T12:00:00Z",
            "authorized_by_login": "avigad",
            "authorization_issue": 101,
            "reason": "test withdrawal",
        }],
    })

    asked = []
    manifest = build_manifest(
        db.path,
        {},
        lambda repository, commit: asked.append((repository, commit)) or ("available", None),
        generated_at=_stamp(),
        database_commit="d" * 40,
    )

    assert ("example/withdrawn-source", "a" * 40) not in asked
    assert manifest["coverage"]["queries_total"] == len(set(asked))


def test_confirmed_archive_loss_keeps_alerting_after_public_status_ages_out(db):
    db.add_entry("PALOMAR-2026-07-29-000002", 1)
    first = build_manifest(
        db.path,
        {},
        lambda _repository, _commit: ("missing", None),
        generated_at=_stamp(-MAX_OBSERVATION_AGE_SECONDS - 2),
        database_commit="d" * 40,
    )
    confirmed = build_manifest(
        db.path,
        first,
        lambda _repository, _commit: ("missing", None),
        generated_at=_stamp(-MAX_OBSERVATION_AGE_SECONDS - 1),
        database_commit="d" * 40,
    )
    carried = build_manifest(
        db.path,
        confirmed,
        lambda _repository, _commit: ("budget", "not checked"),
        generated_at=_stamp(),
        database_commit="d" * 40,
        max_queries=0,
    )

    assert all(row["archive"]["status"] == "unknown" for row in carried["repositories"])
    assert archive_is_degraded(carried)
