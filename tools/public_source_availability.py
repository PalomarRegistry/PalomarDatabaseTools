#!/usr/bin/env python3
"""Refresh source availability using only public target and result documents."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request
from typing import Any

from check_source_availability import (
    ARCHIVE_DEGRADED_EXIT,
    CAPACITY_EXIT,
    GitHubCommitChecker,
    INCOMPLETE_EXIT,
    archive_is_degraded,
    build_manifest,
    utc_now,
)
from source_availability_contract import enforce_refresh_capacity

DEFAULT_ORIGIN = "https://data.palomar-registry.org"
USER_AGENT = "Palomar-source-availability/1"


def _read(url: str) -> tuple[object, str | None]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response), response.headers.get("ETag")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return {}, None
        raise


def _targets(value: object) -> tuple[list[dict[str, str]], str]:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("public source target document has an unsupported schema")
    database_commit = value.get("database_commit")
    rows = value.get("targets")
    if not isinstance(database_commit, str) or not isinstance(rows, list):
        raise ValueError("public source target document is malformed")
    targets: list[dict[str, str]] = []
    for position, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {
            "source_repository", "commit", "fork_repository"
        } or not all(isinstance(item, str) for item in row.values()):
            raise ValueError(f"public source target {position} is malformed")
        targets.append(row)  # type: ignore[arg-type]
    keys = [
        (row["source_repository"].casefold(), row["commit"], row["fork_repository"].casefold())
        for row in targets
    ]
    if keys != sorted(keys) or len(keys) != len(set(keys)):
        raise ValueError("public source targets are duplicated or not canonical")
    return targets, database_commit


def _digest(targets: list[dict[str, str]]) -> str:
    # Match the Worker's explicit AvailabilityTarget construction order. The
    # target document itself is emitted with sorted object keys, so hashing the
    # parsed dictionaries directly would accidentally bind the digest to the
    # producer's presentation order instead of this protocol order.
    protocol_targets = [
        {
            "source_repository": row["source_repository"],
            "commit": row["commit"],
            "fork_repository": row["fork_repository"],
        }
        for row in targets
    ]
    canonical = json.dumps(
        protocol_targets, separators=(",", ":"), ensure_ascii=True
    ).encode() + b"\n"
    return hashlib.sha256(canonical).hexdigest()


def _put(url: str, token: str, body: bytes, etag: str | None) -> None:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "If-Match" if etag is not None else "If-None-Match": etag or "*",
        "User-Agent": USER_AGENT,
    }
    request = urllib.request.Request(url, data=body, method="PUT", headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status != 204:
            raise RuntimeError(f"unexpected source availability response {response.status}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default=DEFAULT_ORIGIN)
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--no-publish", action="store_true")
    args = parser.parse_args(argv)
    origin = args.origin.rstrip("/")
    target_document, _ = _read(f"{origin}/source-availability-targets.json")
    previous, etag = _read(f"{origin}/source-availability.json")
    targets, database_commit = _targets(target_document)
    manifest = build_manifest(
        pathlib.Path("."),
        previous,
        GitHubCommitChecker(os.environ.get("GITHUB_TOKEN", "")),
        generated_at=utc_now(),
        database_commit=database_commit,
        mappings=targets,
    )
    manifest.pop("database_commit", None)
    manifest["schema_version"] = 2
    manifest["targets_sha256"] = _digest(targets)
    prior_revision = previous.get("publication_revision", 0) if isinstance(previous, dict) else 0
    manifest["publication_revision"] = prior_revision + 1 if type(prior_revision) is int else 1
    body = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
    if args.output:
        args.output.write_bytes(body)
    # Both refusals belong to the checker, and this writer went without them
    # when the refresh moved here. A run that exhausted its request budget does
    # not know whether the sources it never reached are still there, and a
    # manifest whose own coverage says it needs longer than the freshness
    # window to come round again is promising a cadence it cannot keep. Neither
    # may be served, and neither is something the endpoint can see: it checks
    # that the rows are the target set's, not that the run behind them
    # finished.
    #
    # After the artifact is written rather than before, so that a run which
    # refuses still leaves the document it would have published to look at.
    if manifest["coverage"]["budget_exhausted"]:
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
    if not args.no_publish:
        token = os.environ.get("PALOMAR_AVAILABILITY_UPDATE_TOKEN", "")
        if len(token) < 32:
            parser.error("PALOMAR_AVAILABILITY_UPDATE_TOKEN must contain at least 32 characters")
        _put(f"{origin}/_operations/source-availability", token, body, etag)
    coverage = manifest["coverage"]
    print(
        f"refreshed {len(targets)} targets; "
        f"{coverage['observations_fresh']} of {coverage['observations_total']} observations fresh"
    )
    return ARCHIVE_DEGRADED_EXIT if archive_is_degraded(manifest) else 0


if __name__ == "__main__":
    raise SystemExit(main())
