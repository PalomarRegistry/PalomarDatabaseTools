"""Executable contract for the mutable source-availability document."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any

SCHEMA_VERSION = 1
STATUS_VALUES = frozenset({"available", "missing", "unknown"})
KNOWN_STATUS_VALUES = frozenset({"available", "missing"})

# The browser already rejects a whole manifest after eighteen hours. Apply the
# same simple limit to every observation so a new top-level timestamp cannot
# make an old carried row authoritative again.
MAX_OBSERVATION_AGE_SECONDS = 18 * 60 * 60
MAX_CLOCK_SKEW_SECONDS = 5 * 60

# This is a Palomar delivery backstop, not an R2 object-size limit. Refresh
# capacity normally binds first; the global document remains acceptable only
# while both limits hold.
MAX_MANIFEST_BYTES = 5 * 1024 * 1024
MAX_REFRESH_CYCLE_HOURS = MAX_OBSERVATION_AGE_SECONDS // (60 * 60)
DEFAULT_QUERY_BUDGET = 800
REFRESH_INTERVAL_HOURS = 6


def parse_timestamp(value: object) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
    except ValueError:
        return None


def format_timestamp(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def observation_age_seconds(checked_at: object, *, as_of: dt.datetime) -> float | None:
    moment = parse_timestamp(checked_at)
    return None if moment is None else (as_of - moment).total_seconds()


def observation_is_fresh(state: Mapping[str, Any], *, as_of: dt.datetime) -> bool:
    age = observation_age_seconds(state.get("checked_at"), as_of=as_of)
    return age is not None and -MAX_CLOCK_SKEW_SECONDS <= age <= MAX_OBSERVATION_AGE_SECONDS


def normalize_observation(
    state: object,
    *,
    as_of: dt.datetime,
    field: str,
) -> dict[str, Any]:
    """Validate an endpoint state and demote an untrustworthy known answer."""
    if not isinstance(state, Mapping):
        raise ValueError(f"{field} is not an object")
    status = state.get("status")
    if status not in STATUS_VALUES:
        raise ValueError(f"{field}.status is unsupported")
    checked_at = state.get("checked_at")
    if checked_at is not None and not isinstance(checked_at, str):
        raise ValueError(f"{field}.checked_at is neither a timestamp nor null")
    last_attempt_at = state.get("last_attempt_at")
    if last_attempt_at is not None and parse_timestamp(last_attempt_at) is None:
        raise ValueError(f"{field}.last_attempt_at is neither a timestamp nor null")
    consecutive_missing = state.get("consecutive_missing")
    if (
        isinstance(consecutive_missing, bool)
        or not isinstance(consecutive_missing, int)
        or consecutive_missing < 0
    ):
        raise ValueError(f"{field}.consecutive_missing is not a non-negative integer")
    last_error = state.get("last_error")
    if last_error is not None and not isinstance(last_error, str):
        raise ValueError(f"{field}.last_error is neither a string nor null")

    normalized = dict(state)
    parsed = parse_timestamp(checked_at)
    if checked_at is not None and parsed is None:
        normalized["checked_at"] = None
    if status not in KNOWN_STATUS_VALUES or observation_is_fresh(normalized, as_of=as_of):
        return normalized

    normalized["status"] = "unknown"
    age = observation_age_seconds(normalized.get("checked_at"), as_of=as_of)
    if last_error is None:
        if age is None:
            normalized["last_error"] = "no valid definitive observation time"
        elif age < -MAX_CLOCK_SKEW_SECONDS:
            normalized["last_error"] = "definitive observation time is too far in the future"
        else:
            normalized["last_error"] = (
                f"definitive observation is older than {MAX_OBSERVATION_AGE_SECONDS} seconds"
            )
    return normalized


def observation_coverage(
    repositories: list[dict[str, Any]],
    *,
    as_of: dt.datetime,
) -> dict[str, Any]:
    total = fresh = stale = unknown = unobserved = 0
    oldest: dt.datetime | None = None
    for position, row in enumerate(repositories):
        for kind in ("original", "archive"):
            state = row[kind]
            total += 1
            moment = parse_timestamp(state.get("checked_at"))
            age = observation_age_seconds(state.get("checked_at"), as_of=as_of)
            if moment is None:
                unobserved += 1
            else:
                oldest = moment if oldest is None or moment < oldest else oldest
                if age is not None and (
                    age > MAX_OBSERVATION_AGE_SECONDS or age < -MAX_CLOCK_SKEW_SECONDS
                ):
                    stale += 1
            if state.get("status") == "unknown":
                unknown += 1
            elif observation_is_fresh(state, as_of=as_of):
                fresh += 1
            else:  # normalize_observation should make this unreachable.
                raise ValueError(
                    f"repositories[{position}].{kind} has a known status without a fresh "
                    "observation"
                )
    oldest_age = None if oldest is None else max(0, int((as_of - oldest).total_seconds()))
    return {
        "coverage_as_of": format_timestamp(as_of),
        "freshness_max_age_seconds": MAX_OBSERVATION_AGE_SECONDS,
        "observations_total": total,
        "observations_fresh": fresh,
        "observations_unknown": unknown,
        "observations_stale": stale,
        "observations_unobserved": unobserved,
        "oldest_observation_at": None if oldest is None else format_timestamp(oldest),
        "oldest_observation_age_seconds": oldest_age,
    }


def normalize_manifest(manifest: object, *, as_of: dt.datetime) -> dict[str, Any]:
    """Return the schema-1 manifest with its per-observation contract enforced."""
    if not isinstance(manifest, Mapping) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("source availability manifest has an unsupported schema")
    repositories = manifest.get("repositories")
    if not isinstance(repositories, list):
        raise ValueError("source availability manifest has no repositories array")

    normalized_rows: list[dict[str, Any]] = []
    for position, row in enumerate(repositories):
        field = f"repositories[{position}]"
        if not isinstance(row, Mapping):
            raise ValueError(f"{field} is not an object")
        for name in ("source_repository", "commit", "fork_repository"):
            if not isinstance(row.get(name), str):
                raise ValueError(f"{field}.{name} is not a string")
        normalized = dict(row)
        for kind in ("original", "archive"):
            normalized[kind] = normalize_observation(
                row.get(kind), as_of=as_of, field=f"{field}.{kind}"
            )
        normalized_rows.append(normalized)

    coverage = manifest.get("coverage")
    if coverage is not None and not isinstance(coverage, Mapping):
        raise ValueError("source availability coverage is not an object")
    normalized_manifest = dict(manifest)
    existing_coverage = dict(coverage) if isinstance(coverage, Mapping) else {}
    query_budget = existing_coverage.get("query_budget", DEFAULT_QUERY_BUDGET)
    if (
        isinstance(query_budget, bool)
        or not isinstance(query_budget, int)
        or query_budget < 0
    ):
        raise ValueError("source availability query_budget is invalid")
    query_keys = {
        (str(repository).casefold(), row["commit"])
        for row in normalized_rows
        for repository in (row["source_repository"], row["fork_repository"])
    }
    queries_total = len(query_keys)
    cycle_runs = (
        (queries_total + query_budget - 1) // query_budget
        if queries_total and query_budget
        else (0 if not queries_total else None)
    )
    normalized_manifest["repositories"] = normalized_rows
    normalized_manifest["coverage"] = {
        **existing_coverage,
        "query_budget": query_budget,
        "queries_total": queries_total,
        "planned_refresh_cycle_runs": cycle_runs,
        "planned_refresh_cycle_hours": (
            None if cycle_runs is None else cycle_runs * REFRESH_INTERVAL_HOURS
        ),
        **observation_coverage(normalized_rows, as_of=as_of),
    }
    return normalized_manifest


def enforce_refresh_capacity(manifest: Mapping[str, Any]) -> None:
    """Refuse a global manifest whose query cycle cannot meet its own SLO."""
    coverage = manifest.get("coverage")
    if not isinstance(coverage, Mapping):
        raise ValueError("source availability coverage is missing")
    queries_total = coverage.get("queries_total")
    cycle_hours = coverage.get("planned_refresh_cycle_hours")
    if (
        isinstance(queries_total, bool)
        or not isinstance(queries_total, int)
        or queries_total < 0
    ):
        raise ValueError("source availability queries_total is invalid")
    if queries_total == 0 and cycle_hours == 0:
        return
    if (
        isinstance(cycle_hours, bool)
        or not isinstance(cycle_hours, int)
        or cycle_hours > MAX_REFRESH_CYCLE_HOURS
    ):
        raise ValueError(
            "source availability needs more than "
            f"{MAX_REFRESH_CYCLE_HOURS} hours to refresh; shard or increase "
            "measured refresh capacity before publishing"
        )
