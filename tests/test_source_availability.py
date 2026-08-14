"""Operational source-availability manifest behavior."""

from __future__ import annotations

import json
from types import SimpleNamespace

import check_source_availability as availability
import pytest
from check_source_availability import build_manifest


def test_two_definitive_failures_are_required_before_missing(db):
    db.add_entry("PALOMAR-2026-07-29-000002", 1)
    calls = []

    def missing(repository, commit):
        calls.append((repository, commit))
        return "missing", None

    first = build_manifest(
        db.path,
        {},
        missing,
        generated_at="2026-08-06T00:00:00Z",
        database_commit="1" * 40,
    )
    assert all(row["original"]["status"] == "unknown" for row in first["repositories"])
    assert all(row["archive"]["status"] == "unknown" for row in first["repositories"])
    second = build_manifest(
        db.path,
        first,
        missing,
        generated_at="2026-08-06T06:00:00Z",
        database_commit="1" * 40,
    )
    assert all(row["original"]["status"] == "missing" for row in second["repositories"])
    assert all(row["archive"]["status"] == "missing" for row in second["repositories"])
    assert len(calls) == len({*calls}) * 2


def test_transient_failure_retains_last_definitive_state(db):
    db.add_entry("PALOMAR-2026-07-29-000002", 1)
    available = build_manifest(
        db.path,
        {},
        lambda _repository, _commit: ("available", None),
        generated_at="2026-08-06T00:00:00Z",
        database_commit="1" * 40,
    )
    transient = build_manifest(
        db.path,
        available,
        lambda _repository, _commit: ("transient", "HTTP 503"),
        generated_at="2026-08-06T06:00:00Z",
        database_commit="1" * 40,
    )
    for row in transient["repositories"]:
        assert row["original"]["status"] == "available"
        assert row["original"]["checked_at"] == "2026-08-06T00:00:00Z"
        assert row["original"]["last_error"] == "HTTP 503"


def test_manifest_deduplicates_the_same_commit_across_entries(db):
    db.add_entry("PALOMAR-2026-07-29-000001", 2)
    calls = []

    def available(repository, commit):
        calls.append((repository.casefold(), commit))
        return "available", None

    manifest = build_manifest(
        db.path,
        json.loads("{}"),
        available,
        generated_at="2026-08-06T00:00:00Z",
        database_commit="1" * 40,
    )
    assert len(calls) == len(set(calls))
    assert len(manifest["repositories"]) == len({(row["source_repository"].casefold(), row["commit"]) for row in manifest["repositories"]})


def test_every_entry_must_supply_preservation_mappings(db):
    path = "entries/PALOMAR-2026-07-29-000001-v1.json"
    entry = db.read_json(path)
    entry.pop("preservation")
    db.write_json(path, entry)

    with pytest.raises(TypeError, match="has no preservation mappings"):
        availability.preservation_rows(db.path)


def test_cli_uses_the_builtin_actions_token(db, tmp_path, monkeypatch):
    tokens = []

    class RecordingChecker:
        def __init__(self, token):
            tokens.append(token)

    monkeypatch.setenv("GITHUB_TOKEN", "short-lived-actions-token")
    monkeypatch.setenv("PALOMAR_SOURCE_HEALTH_TOKEN", "obsolete-operator-secret")
    monkeypatch.setattr(availability, "GitHubCommitChecker", RecordingChecker)
    monkeypatch.setattr(
        availability,
        "build_manifest",
        lambda _root, _previous, _checker, **_kwargs: {
            "schema_version": 1,
            "repositories": [],
            "coverage": {
                "queries_total": 0, "queries_attempted": 0, "queries_answered": 0,
                "queries_skipped": 0, "budget_exhausted": False,
                "planned_refresh_cycle_hours": 0,
                "observations_fresh": 0, "observations_total": 0,
                "oldest_observation_at": None,
            },
        },
    )
    monkeypatch.setattr(
        availability.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="1" * 40 + "\n"),
    )
    output = tmp_path / "availability.json"
    assert availability.main(["--root", str(db.path), "--output", str(output)]) == 0
    assert tokens == ["short-lived-actions-token"]


def test_cli_models_a_missing_previous_file_as_no_prior_observations(
    db, tmp_path, monkeypatch
):
    previous_values = []

    def record_previous(_root, previous, _checker, **_kwargs):
        previous_values.append(previous)
        return {
            "schema_version": 1,
            "repositories": [],
            "coverage": {
                "queries_total": 0,
                "queries_attempted": 0,
                "queries_answered": 0,
                "queries_skipped": 0,
                "budget_exhausted": False,
                "planned_refresh_cycle_hours": 0,
                "observations_fresh": 0,
                "observations_total": 0,
                "oldest_observation_at": None,
            },
        }

    monkeypatch.setattr(availability, "GitHubCommitChecker", lambda _token: object())
    monkeypatch.setattr(availability, "build_manifest", record_previous)
    monkeypatch.setattr(
        availability.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="1" * 40 + "\n"),
    )
    missing = tmp_path / "not-created-on-a-cold-bucket.json"
    output = tmp_path / "availability.json"

    assert availability.main([
        "--root", str(db.path), "--previous", str(missing), "--output", str(output)
    ]) == 0
    assert previous_values == [{}]
    assert output.is_file()
