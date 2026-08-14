#!/usr/bin/env python3
"""Publish the active-only source availability manifest.

This file is the one public object that changes without the dataset changing.
It is rewritten in place at a stable key rather than being carried in a release,
so that refreshing it four times a day does not mint a new release id and
rewrite everything else along with it.

Nothing cross-references it. It carries its own observation time, every known
row is limited to the same eighteen-hour freshness window as the document, and
it never names a Palomar identifier. So it does not need the release pointer to
be consistent with anything, and it must not be able to move the pointer.

What it does need is a write order, because two publishers touch it: the
six-hourly availability refresh and any database publication. Observation time
cannot supply that -- a takedown rewrites the rows while preserving the
timestamp it observed -- so publication order is carried separately.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import tempfile
from typing import Any

from publish_snapshot import (
    AVAILABILITY_PATH,
    BUCKET_DEFAULT,
    PUBLIC_PREFIX,
    _put_object,
    _read_object,
    _sha256,
    _verify,
)
from source_availability_contract import (
    MAX_CLOCK_SKEW_SECONDS,
    MAX_MANIFEST_BYTES,
    MAX_OBSERVATION_AGE_SECONDS,
    enforce_refresh_capacity,
    normalize_manifest,
    parse_timestamp,
)
from registration_projection import all_entry_summaries
from takedowns import load_takedowns

REVISION_KEY = "publication_revision"
KEY = f"{PUBLIC_PREFIX}{AVAILABILITY_PATH}"


def _content(manifest: dict[str, Any]) -> dict[str, Any]:
    """The observation identity, excluding publication-time derived ages."""
    content = {key: value for key, value in manifest.items() if key != REVISION_KEY}
    coverage = content.get("coverage")
    if isinstance(coverage, dict):
        content["coverage"] = {
            key: value
            for key, value in coverage.items()
            if key not in {"coverage_as_of", "oldest_observation_age_seconds"}
        }
    return content


def _age_seconds(manifest: dict[str, Any], *, now: dt.datetime) -> float | None:
    moment = parse_timestamp(manifest.get("generated_at"))
    return None if moment is None else (now - moment).total_seconds()


def _decode_manifest(raw: bytes, where: str) -> object:
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{where} is not valid JSON") from error


def _select_active_availability(
    availability: object,
    active_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Keep only observations named by currently served preservation maps."""
    wanted: set[tuple[str, str, str]] = set()
    for entry in active_entries:
        preservation = entry.get("preservation")
        if not isinstance(preservation, dict):
            raise ValueError(
                f"{entry.get('id', 'entry')}: current entries must carry preservation"
            )
        rows = preservation.get("repositories")
        if not isinstance(rows, list):
            raise ValueError(
                f"{entry.get('id', 'entry')}: preservation must contain repositories"
            )
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(
                    f"{entry.get('id', 'entry')}: preservation contains a malformed mapping"
                )
            values = (
                row.get("source_repository"),
                row.get("commit"),
                row.get("fork_repository"),
            )
            if not all(isinstance(value, str) for value in values):
                raise ValueError(
                    f"{entry.get('id', 'entry')}: preservation contains a malformed mapping"
                )
            wanted.add(values)  # type: ignore[arg-type]
    if not isinstance(availability, dict) or availability.get("schema_version") != 1:
        raise ValueError("source availability manifest has an unsupported schema")
    repositories = availability.get("repositories")
    if not isinstance(repositories, list):
        raise ValueError("source availability manifest has no repositories array")
    selected = []
    for row in repositories:
        if not isinstance(row, dict):
            raise ValueError("source availability manifest contains a malformed row")
        key = (
            row.get("source_repository"),
            row.get("commit"),
            row.get("fork_repository"),
        )
        if key in wanted:
            selected.append(row)
    filtered = {
        "schema_version": 1,
        "generated_at": availability.get("generated_at"),
        "coverage": availability.get("coverage", {}),
        "repositories": selected,
    }
    generated_at = parse_timestamp(filtered["generated_at"])
    if generated_at is None:
        raise ValueError("source availability manifest has a malformed generated_at")
    return normalize_manifest(filtered, as_of=generated_at)


def active_availability(root: pathlib.Path, manifest: pathlib.Path) -> dict[str, Any]:
    """The observed manifest, cut down to the records that are being served.

    This used to be a side effect of staging a whole release: every record,
    render and evidence file copied and re-hashed, four times a day, to produce
    one small object that cross-references none of them. The rows it keeps are
    decided by the active entries' preservation mappings, and those are all it
    ever needed.
    """
    entries_by_target = {}
    for summary in all_entry_summaries(root):
        entry = json.loads((root / summary["path"]).read_text(encoding="utf-8"))
        entries_by_target[(entry["id"], entry["version"])] = (summary, entry)
    rows, errors = load_takedowns(root, entries_by_target)
    if errors:
        raise ValueError("invalid takedowns.json:\n" + "\n".join(errors))
    taken_down = {(row["id"], row["version"]) for row in rows}
    active = [
        entry for target, (_summary, entry) in entries_by_target.items()
        if target not in taken_down
    ]
    observed = _decode_manifest(
        manifest.read_bytes(), "source availability manifest"
    )
    return _select_active_availability(observed, active)


def write_current_availability(
    client: Any,
    bucket: str,
    target: pathlib.Path,
) -> bool:
    """Materialize the served manifest, or represent its absence by no file."""
    raw = _read_object(client, bucket=bucket, key=KEY)
    if raw is None:
        # RUNNER_TEMP is normally fresh, but removing a pre-existing target
        # keeps the filesystem handoff truthful when this command is retried.
        target.unlink(missing_ok=True)
        return False
    pending: pathlib.Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=target.parent,
            prefix=f".{target.name}.",
            delete=False,
        ) as stream:
            pending = pathlib.Path(stream.name)
            stream.write(raw)
        pending.replace(target)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)
    return True


def validate_retained_availability(
    manifest: pathlib.Path,
    *,
    now: dt.datetime | None = None,
) -> None:
    """Validate a served manifest that an accepted addition leaves unchanged.

    A closed accepted delta cannot withdraw or alter any preservation mapping
    already represented by the operational manifest. It therefore needs no
    whole-registry active-row filter, but retaining the object must not make a
    malformed or over-capacity live document invisible to publication.
    """
    raw = manifest.read_bytes()
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError(
            f"served source availability manifest is {len(raw)} bytes; "
            f"the global delivery budget is {MAX_MANIFEST_BYTES} bytes"
        )
    candidate = _decode_manifest(raw, "served source availability manifest")
    if not isinstance(candidate, dict) or candidate.get("schema_version") != 1:
        raise ValueError("served source availability manifest has an unsupported schema")
    if not isinstance(candidate.get("repositories"), list):
        raise ValueError("served source availability manifest has no repositories array")
    if parse_timestamp(candidate.get("generated_at")) is None:
        raise ValueError("served source availability manifest has a malformed generated_at")
    now = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC).replace(microsecond=0)
    try:
        normalized = normalize_manifest(candidate, as_of=now)
        enforce_refresh_capacity(normalized)
    except ValueError as error:
        raise ValueError(
            f"served source availability manifest is invalid: {error}"
        ) from error
    revision = candidate.get(REVISION_KEY)
    if type(revision) is not int or revision < 1:
        raise ValueError(
            "served source availability manifest has an invalid publication_revision"
        )


def publish_availability(
    client: Any,
    bucket: str,
    site: pathlib.Path,
    *,
    now: dt.datetime | None = None,
) -> str:
    """Write the staged manifest if it says something new. Returns what happened."""
    staged = _decode_manifest(
        (site / AVAILABILITY_PATH).read_bytes(), "staged source availability manifest"
    )
    if not isinstance(staged, dict) or staged.get("schema_version") != 1:
        raise ValueError("staged source availability has an unsupported schema")
    if not isinstance(staged.get("repositories"), list):
        raise ValueError("staged source availability has no repositories array")
    coverage = staged.get("coverage")
    if isinstance(coverage, dict) and coverage.get("budget_exhausted"):
        # It could not ask, so it does not know. Replacing what is live with
        # that would turn "we checked and it is fine" into "we have no idea",
        # while looking freshly generated.
        print(
            "::warning::source availability could not be established this run; "
            "leaving the live one"
        )
        return "incomplete"
    now = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC).replace(microsecond=0)
    age = _age_seconds(staged, now=now)
    if age is None:
        raise ValueError("staged source availability has a malformed generated_at")
    if age > MAX_OBSERVATION_AGE_SECONDS or age < -MAX_CLOCK_SKEW_SECONDS:
        # The website discards a manifest this old, so publishing it would only
        # replace one thing nobody can use with another. Not worth a red run.
        print(f"::warning::source availability is {age / 3600:.1f}h old; leaving the live one")
        return "stale"
    staged = normalize_manifest(staged, as_of=now)
    enforce_refresh_capacity(staged)

    raw = _read_object(client, bucket, KEY)
    live = None
    live_revision = 0
    if raw is not None:
        candidate = _decode_manifest(raw, "served source availability manifest")
        if not isinstance(candidate, dict) or candidate.get("schema_version") != 1:
            raise ValueError(
                "served source availability manifest has an unsupported schema"
            )
        if not isinstance(candidate.get("repositories"), list):
            raise ValueError("served source availability manifest has no repositories array")
        try:
            normalize_manifest(candidate, as_of=now)
        except ValueError as error:
            raise ValueError(
                f"served source availability manifest is invalid: {error}"
            ) from error
        revision_value = candidate.get(REVISION_KEY)
        if type(revision_value) is not int or revision_value < 1:
            raise ValueError(
                "served source availability manifest has an invalid publication_revision"
            )
        live = candidate
        live_revision = revision_value
    unchanged = live is not None and _content(live) == _content(staged)
    revision = live_revision if unchanged and live_revision > 0 else live_revision + 1
    body = json.dumps(
        {**_content(staged), REVISION_KEY: revision}, indent=2, sort_keys=True
    ).encode() + b"\n"
    if len(body) > MAX_MANIFEST_BYTES:
        raise ValueError(
            f"source availability manifest is {len(body)} bytes; "
            f"the global delivery budget is {MAX_MANIFEST_BYTES} bytes"
        )
    if unchanged:
        print("source availability is unchanged")
        return "unchanged"
    digest = _sha256(body)
    _put_object(client, bucket, KEY, body, digest, AVAILABILITY_PATH)
    _verify(client, bucket, KEY, body, digest)
    print(f"published source availability revision {revision}")
    return "published"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument(
        "--manifest",
        type=pathlib.Path,
        help="the observed manifest to filter to the active records of --root and publish",
    )
    parser.add_argument(
        "--root",
        type=pathlib.Path,
        default=pathlib.Path("."),
        help="the canonical database, read with --manifest to decide which rows are active",
    )
    operation.add_argument(
        "--write-current",
        type=pathlib.Path,
        help="atomically write the manifest being served here, or remove the target "
        "if none is served, so that the next run can carry forward what it knows",
    )
    operation.add_argument(
        "--validate-retained",
        type=pathlib.Path,
        help="validate a served manifest that a closed accepted delta leaves unchanged; "
        "this operation needs no bucket credentials or canonical record scan",
    )
    parser.add_argument("--bucket", default=os.environ.get("R2_BUCKET", BUCKET_DEFAULT))
    args = parser.parse_args()
    if args.validate_retained is not None:
        validate_retained_availability(args.validate_retained)
        print("retained source availability is valid")
        return 0
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    access_key = os.environ.get("R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY")
    if not all((account_id, access_key, secret_key)):
        parser.error(
            "CLOUDFLARE_ACCOUNT_ID, R2_ACCESS_KEY_ID, and R2_SECRET_ACCESS_KEY are required"
        )
    import boto3

    client = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
    )
    if args.write_current is not None:
        present = write_current_availability(client, args.bucket, args.write_current)
        print("read the manifest being served" if present else "nothing is served yet")
        return 0

    # Filtered here rather than by staging a release. Staging built a tree of
    # every record, render and evidence file in the registry to produce this
    # one small object, four times a day: twenty-five files an entry, copied and
    # hashed, for a file that cross-references none of them.
    assert args.manifest is not None
    with tempfile.TemporaryDirectory(prefix="palomar-availability-") as directory:
        site = pathlib.Path(directory)
        (site / AVAILABILITY_PATH).write_text(
            json.dumps(active_availability(args.root, args.manifest), indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        publish_availability(client, args.bucket, site)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
