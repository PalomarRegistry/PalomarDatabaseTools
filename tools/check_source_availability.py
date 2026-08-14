#!/usr/bin/env python3
"""Publish the operational availability of original and preserved Git commits."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from source_availability_contract import (
    DEFAULT_QUERY_BUDGET,
    REFRESH_INTERVAL_HOURS,
    STATUS_VALUES,
    enforce_refresh_capacity,
    normalize_manifest,
    normalize_observation,
    parse_timestamp,
)
from takedowns import load_takedowns

API_ROOT = "https://api.github.com"
DEFINITIVE_MISSING = frozenset({404, 409})
ARCHIVE_DEGRADED_EXIT = 3
INCOMPLETE_EXIT = 4
CAPACITY_EXIT = 5
# GITHUB_TOKEN gets a thousand requests an hour, and every entry contributes
# roughly seventeen preservation rows, each asked about twice. Re-asking every
# row every run therefore stopped fitting in the budget at about thirty
# entries, and the answer to running out was to write "unknown" everywhere and
# exit zero. A run now asks about a bounded number of rows, oldest first, and
# says what it did not get to.
MAX_QUERIES_PER_RUN = DEFAULT_QUERY_BUDGET


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def repository_key(repository: str, commit: str) -> str:
    return f"{repository.casefold()}\0{commit}"


class GitHubCommitChecker:
    """Ask GitHub whether a commit is still there.

    A refused request is classified apart from a failed one. Retrying a rate
    limit spends more of the budget that was just exhausted, and the reset can
    be an hour away, so the first refusal stops the run: every later query
    would answer "unknown" too, and a manifest that knows nothing must not look
    like one that checked.
    """

    def __init__(self, token: str = "") -> None:
        self.token = token
        self.budget_exhausted = False
        self.provider_query_limit: int | None = None
        self.provider_queries_remaining: int | None = None
        self.provider_queries_attempted = 0
        self._rate_lock = threading.Lock()

    def _begin_query(self) -> bool:
        with self._rate_lock:
            if self.budget_exhausted:
                return False
            self.provider_queries_attempted += 1
            return True

    def _record_capacity(self, headers: Mapping[str, Any]) -> None:
        try:
            limit = int(headers.get("x-ratelimit-limit"))
        except (TypeError, ValueError):
            limit = None
        try:
            remaining = int(headers.get("x-ratelimit-remaining"))
        except (TypeError, ValueError):
            remaining = None
        with self._rate_lock:
            if limit is not None:
                self.provider_query_limit = limit
            if remaining is not None:
                self.provider_queries_remaining = (
                    remaining
                    if self.provider_queries_remaining is None
                    else min(self.provider_queries_remaining, remaining)
                )

    def __call__(self, repository: str, commit: str) -> tuple[str, str | None]:
        if not self._begin_query():
            return "budget", "the request budget was already exhausted"
        url = f"{API_ROOT}/repos/{repository}/git/commits/{commit}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "Palomar-source-availability",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        last_error = "GitHub request failed"
        for attempt in range(3):
            try:
                with urllib.request.urlopen(
                    urllib.request.Request(url, headers=headers), timeout=20
                ) as response:
                    self._record_capacity(response.headers)
                    if response.status == 200:
                        return "available", None
                    last_error = f"unexpected HTTP {response.status}"
            except urllib.error.HTTPError as error:
                self._record_capacity(error.headers or {})
                if error.code in DEFINITIVE_MISSING:
                    return "missing", None
                error_headers = error.headers or {}
                remaining = error_headers.get("x-ratelimit-remaining")
                retry_after = error_headers.get("retry-after")
                if error.code in (403, 429) and (
                    str(remaining) == "0" or retry_after is not None
                ):
                    with self._rate_lock:
                        self.budget_exhausted = True
                    return "budget", f"HTTP {error.code}, request budget exhausted"
                last_error = f"HTTP {error.code}"
            except (OSError, TimeoutError) as error:
                last_error = type(error).__name__
            if attempt < 2:
                time.sleep(2**attempt)
        return "transient", last_error


def _previous_states(
    previous: object,
    *,
    as_of: dt.datetime,
) -> dict[tuple[str, str], Mapping[str, Any]]:
    states: dict[tuple[str, str], Mapping[str, Any]] = {}
    if not isinstance(previous, dict) or previous.get("schema_version") not in (1, 2):
        return states
    repositories = previous.get("repositories")
    if not isinstance(repositories, list):
        return states
    for row in repositories:
        if not isinstance(row, dict):
            continue
        source = row.get("source_repository")
        commit = row.get("commit")
        fork = row.get("fork_repository")
        if not all(isinstance(value, str) for value in (source, commit, fork)):
            continue
        for kind, repository in (("original", source), ("archive", fork)):
            state = row.get(kind)
            if isinstance(state, dict) and state.get("status") in STATUS_VALUES:
                try:
                    states[(kind, repository_key(repository, commit))] = normalize_observation(
                        state,
                        as_of=as_of,
                        field=f"previous.{kind}",
                    )
                except ValueError:
                    # A malformed served state is not evidence to carry into a
                    # new manifest. Re-query it as though it had never existed.
                    continue
    return states


def _transition(
    previous: Mapping[str, Any] | None,
    outcome: tuple[str, str | None],
    checked_at: str,
) -> dict[str, Any]:
    old = previous or {}
    result, error = outcome
    if result == "available":
        return {
            "status": "available",
            "checked_at": checked_at,
            "last_attempt_at": checked_at,
            "consecutive_missing": 0,
            "last_error": None,
        }
    if result == "missing":
        count = int(old.get("consecutive_missing") or 0) + 1
        confirmed = count >= 2
        return {
            "status": "missing" if confirmed else "unknown",
            "checked_at": checked_at if confirmed else old.get("checked_at"),
            "last_attempt_at": checked_at,
            "consecutive_missing": count,
            "last_error": None,
        }
    if result == "budget":
        # Not attempted, so not even an attempt: leave the row exactly as it
        # was and let coverage report that it was not revisited.
        return dict(old) if old else {
            "status": "unknown",
            "checked_at": None,
            "last_attempt_at": None,
            "consecutive_missing": 0,
            "last_error": "not checked: the request budget was exhausted",
        }
    return {
        "status": old.get("status") if old.get("status") in STATUS_VALUES else "unknown",
        "checked_at": old.get("checked_at"),
        "last_attempt_at": checked_at,
        "consecutive_missing": int(old.get("consecutive_missing") or 0),
        "last_error": error or "transient GitHub API failure",
    }


def preservation_rows(root: pathlib.Path) -> list[dict[str, str]]:
    takedowns, errors = load_takedowns(root)
    if errors:
        raise ValueError("invalid takedowns.json:\n" + "\n".join(errors))
    taken_down = {(row["id"], row["version"]) for row in takedowns}
    rows: dict[str, dict[str, str]] = {}
    entries = root / "entries"
    for path in sorted(entries.glob("*.json") if entries.is_dir() else []):
        entry = json.loads(path.read_text(encoding="utf-8"))
        if (entry.get("id"), entry.get("version")) in taken_down:
            continue
        preservation = entry.get("preservation")
        mappings = preservation.get("repositories") if isinstance(preservation, dict) else None
        if not isinstance(mappings, list):
            raise TypeError(f"{path.name} has no preservation mappings")
        for mapping in mappings:
            if not isinstance(mapping, dict):
                raise TypeError(f"{path.name} has a malformed preservation mapping")
            source = mapping.get("source_repository")
            commit = mapping.get("commit")
            fork = mapping.get("fork_repository")
            if not all(isinstance(value, str) for value in (source, commit, fork)):
                raise ValueError(f"{path.name} has a malformed preservation mapping")
            key = repository_key(source, commit)
            candidate = {
                "source_repository": source,
                "commit": commit,
                "fork_repository": fork,
            }
            if key in rows and rows[key] != candidate:
                raise ValueError(f"conflicting archive mappings for {source}@{commit}")
            rows[key] = candidate
    return sorted(
        rows.values(),
        key=lambda row: (row["source_repository"].casefold(), row["commit"]),
    )


def build_manifest(
    root: pathlib.Path,
    previous: object,
    checker: Callable[[str, str], tuple[str, str | None]],
    *,
    generated_at: str,
    database_commit: str,
    max_queries: int = MAX_QUERIES_PER_RUN,
    mappings: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    generated_moment = parse_timestamp(generated_at)
    if generated_moment is None:
        raise ValueError("generated_at is not a UTC timestamp")
    if max_queries < 0:
        raise ValueError("max_queries must be non-negative")
    mappings = preservation_rows(root) if mappings is None else mappings
    prior = _previous_states(previous, as_of=generated_moment)
    queries_by_key: dict[str, tuple[str, str]] = {}
    for row in mappings:
        for repository in (row["source_repository"], row["fork_repository"]):
            candidate = (repository, row["commit"])
            key = repository_key(*candidate)
            existing = queries_by_key.get(key)
            if existing is None or (
                candidate[0].casefold(), candidate[0], candidate[1]
            ) < (existing[0].casefold(), existing[0], existing[1]):
                queries_by_key[key] = candidate
    queries = list(queries_by_key.values())

    def staleness(item: tuple[str, str]) -> tuple[int, str, str, str, str]:
        """Never-checked first, then oldest-checked, then a stable tiebreak."""
        repository, commit = item
        key = repository_key(repository, commit)
        states = [
            state
            for kind in ("original", "archive")
            if (state := prior.get((kind, key))) is not None
        ]
        checked = sorted(
            str(state["checked_at"])
            for state in states
            if parse_timestamp(state.get("checked_at")) is not None
        )
        urgent = not states or any(
            state.get("status") == "unknown" or not state.get("checked_at") for state in states
        )
        return (
            0 if urgent else 1,
            checked[0] if checked else "",
            repository.casefold(),
            repository,
            commit,
        )

    ordered = sorted(queries, key=staleness)
    selected = ordered[:max_queries]

    outcomes: dict[str, tuple[str, str | None]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(checker, repository, commit): (repository, commit)
            for repository, commit in selected
        }
        for future, (repository, commit) in futures.items():
            outcomes[repository_key(repository, commit)] = future.result()
    for repository, commit in ordered[len(selected):]:
        outcomes[repository_key(repository, commit)] = ("budget", "not checked this run")

    repositories = []
    for row in mappings:
        source = row["source_repository"]
        commit = row["commit"]
        fork = row["fork_repository"]
        repositories.append(
            {
                **row,
                "original": _transition(
                    prior.get(("original", repository_key(source, commit))),
                    outcomes[repository_key(source, commit)],
                    generated_at,
                ),
                "archive": _transition(
                    prior.get(("archive", repository_key(fork, commit))),
                    outcomes[repository_key(fork, commit)],
                    generated_at,
                ),
            }
        )
    answered = sum(
        1 for outcome in outcomes.values() if outcome[0] in ("available", "missing")
    )
    attempted = getattr(
        checker,
        "provider_queries_attempted",
        sum(1 for outcome in outcomes.values() if outcome[0] != "budget"),
    )
    cycle_runs = (
        (len(ordered) + max_queries - 1) // max_queries
        if ordered and max_queries
        else (0 if not ordered else None)
    )
    manifest = {
        "schema_version": 1,
        "generated_at": generated_at,
        "database_commit": database_commit,
        # What this run actually established, so that a manifest which could
        # not ask is not mistaken for one that asked and got good news.
        "coverage": {
            "query_budget": max_queries,
            "queries_total": len(ordered),
            "queries_selected": len(selected),
            "queries_attempted": attempted,
            "queries_answered": answered,
            "queries_skipped": len(ordered) - attempted,
            "budget_exhausted": bool(getattr(checker, "budget_exhausted", False)),
            "provider_query_limit": getattr(checker, "provider_query_limit", None),
            "provider_queries_remaining": getattr(
                checker, "provider_queries_remaining", None
            ),
            "planned_refresh_cycle_runs": cycle_runs,
            "planned_refresh_cycle_hours": (
                None if cycle_runs is None else cycle_runs * REFRESH_INTERVAL_HOURS
            ),
        },
        "repositories": repositories,
    }
    return normalize_manifest(manifest, as_of=generated_moment)


def archive_is_degraded(manifest: Mapping[str, Any]) -> bool:
    """Keep alerting after a confirmed loss ages out of public authority."""
    return any(
        row["archive"]["status"] == "missing"
        or row["archive"].get("consecutive_missing", 0) >= 2
        for row in manifest["repositories"]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=pathlib.Path, default=pathlib.Path("."))
    parser.add_argument("--previous", type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args(argv)
    previous: object = {}
    if args.previous and args.previous.is_file():
        try:
            previous = json.loads(args.previous.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
    database_commit = subprocess.run(
        ["git", "-C", str(args.root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    manifest = build_manifest(
        args.root,
        previous,
        GitHubCommitChecker(os.environ.get("GITHUB_TOKEN", "")),
        generated_at=utc_now(),
        database_commit=database_commit,
    )
    args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    coverage = manifest["coverage"]
    print(
        f"attempted {coverage['queries_attempted']} of {coverage['queries_total']} queries, "
        f"{coverage['queries_answered']} answered, {coverage['queries_skipped']} skipped; "
        f"{coverage['observations_fresh']} of "
        f"{coverage['observations_total']} observations fresh; "
        f"oldest observation {coverage['oldest_observation_at'] or 'never'}"
    )
    if coverage["budget_exhausted"]:
        print(
            "::error::the GitHub request budget was exhausted, so this run does not know "
            "whether the sources are still there",
            file=sys.stderr,
        )
        return INCOMPLETE_EXIT
    try:
        enforce_refresh_capacity(manifest)
    except ValueError as error:
        print(f"::error::{error}", file=sys.stderr)
        return CAPACITY_EXIT
    return (
        ARCHIVE_DEGRADED_EXIT
        if archive_is_degraded(manifest)
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
