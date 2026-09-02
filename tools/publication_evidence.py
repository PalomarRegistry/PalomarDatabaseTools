#!/usr/bin/env python3
"""Select and validate one release delta's post-publication health work."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import sys
from typing import Any

import release_delta
from takedowns import committed_manifest_blob

COMMIT_RE = re.compile(r"[0-9a-f]{40}")
IDENTIFIER = r"PALOMAR-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}"
VERSION_RE = re.compile(rf"({IDENTIFIER})-v([1-9][0-9]*)")
TOMBSTONE_PATH_RE = re.compile(rf"tombstones/({IDENTIFIER}-v[1-9][0-9]*)\.json")
ENTRY_PATH_RE = re.compile(rf"entries/({IDENTIFIER})-v([1-9][0-9]*)\.json")
VERSIONED_PATH_RE = re.compile(
    rf"(?:entries/({IDENTIFIER})-v([1-9][0-9]*)\.json"
    rf"|(?:renders|evidence)/({IDENTIFIER})-v([1-9][0-9]*)/.+)"
)


def _commit(value: str) -> str:
    if not COMMIT_RE.fullmatch(value):
        raise ValueError("database commit must be a lowercase 40-character git object id")
    return value


def _read_delta(path: pathlib.Path) -> dict[str, Any]:
    raw = path.read_bytes()
    delta = release_delta.parse(raw)
    if raw != release_delta.canonical_bytes(delta):
        raise ValueError("release-delta.json is valid but is not in its canonical byte form")
    return delta


def _version(path: str) -> str | None:
    match = VERSIONED_PATH_RE.fullmatch(path)
    if match is None:
        return None
    identifier = match.group(1) or match.group(3)
    version = match.group(2) or match.group(4)
    return f"{identifier}-v{version}"


def versions_in(delta: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return affected active versions and the withdrawals this event must re-prove.

    Most immutable additions accompany a newly added entry. A maintainer
    correction may also commit a previously published version's historical
    render or evidence as a sparse validation dependency. That version is not
    new, but the newly published objects still need proportional health, so
    every immutable addition contributes its version. A full release re-proves
    every current stable tombstone, including
    the immediate deletion that caused a takedown publication. A closed
    incremental release has proved the authority blob byte-identical to its
    authenticated parent and writes no tombstones, so daily/manual health retains
    the exhaustive recheck instead of charging it to every later acceptance.
    The authoritative delta parser has already closed every row shape.
    """
    entry_versions = [
        f"{match.group(1)}-v{match.group(2)}"
        for row in delta["additions"]
        if (match := ENTRY_PATH_RE.fullmatch(row["path"]))
    ]
    if entry_versions != sorted(set(entry_versions)):
        raise ValueError("release additions contain duplicate or unsorted entry versions")
    version_set: set[str] = set()
    for row in delta["additions"]:
        target = _version(row["path"])
        if target is None:
            raise ValueError(f"release addition has an unrecognised record path: {row['path']}")
        version_set.add(target)
    versions = sorted(version_set)

    tombstones = [
        match.group(1)
        for row in delta["stable"]
        if (match := TOMBSTONE_PATH_RE.fullmatch(row["path"]))
    ]
    if delta["parent"] is not None and tombstones:
        raise ValueError("an incremental accepted release cannot write tombstones")
    if delta["parent"] is not None and delta["withdrawals"]:
        raise ValueError("an incremental accepted release cannot withdraw records")
    if any(target in version_set for target in tombstones):
        raise ValueError("a release version cannot be both added and taken down")
    withdrawn = tombstones if delta["parent"] is None else []
    return versions, withdrawn


def _record(path: pathlib.Path, target: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"publication health target is missing or symbolic: {path}")
    raw = path.read_bytes()
    return _record_bytes(raw, target, str(path))


def _record_bytes(raw: bytes, target: str, source: str) -> bytes:
    match = VERSION_RE.fullmatch(target)
    assert match is not None
    value = json.loads(raw)
    if not isinstance(value, dict) or (value.get("id"), value.get("version")) != (
        match.group(1),
        int(match.group(2)),
    ):
        raise ValueError(f"publication health target disagrees with its path: {source}")
    return raw


def _addition_rows(delta: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        f"{match.group(1)}-v{match.group(2)}": row
        for row in delta["additions"]
        if (match := ENTRY_PATH_RE.fullmatch(row["path"]))
    }


def _matches_row(raw: bytes, row: dict[str, Any]) -> bool:
    return len(raw) == row["bytes"] and hashlib.sha256(raw).hexdigest() == row["sha256"]


def _committed_record(database: pathlib.Path, commit: str, target: str) -> bytes:
    """Read one ordinary entry blob exactly from the release's Git commit."""
    relative = f"entries/{target}.json"
    listed = subprocess.run(
        ["git", "-C", str(database), "ls-tree", "-z", commit, "--", relative],
        check=False,
        capture_output=True,
    )
    if listed.returncode:
        raise ValueError(f"committed publication health target cannot be resolved: {relative}")
    metadata, separator, found_path = listed.stdout.partition(b"\t")
    fields = metadata.split()
    if (
        separator != b"\t"
        or found_path.rstrip(b"\0") != relative.encode()
        or len(fields) != 3
        or fields[0] != b"100644"
        or fields[1] != b"blob"
    ):
        raise ValueError(
            f"committed publication health target is missing or not an ordinary file: {relative}"
        )
    blob = subprocess.run(
        ["git", "-C", str(database), "cat-file", "blob", fields[2].decode("ascii")],
        check=False,
        capture_output=True,
    )
    if blob.returncode:
        raise ValueError(f"committed publication health target cannot be read: {relative}")
    return blob.stdout


def _bundle_records(
    delta: dict[str, Any],
    entries: pathlib.Path,
    destination: pathlib.Path,
    targets: list[str],
    *,
    database: pathlib.Path | None,
    commit: str,
    canonical_targets: set[str],
) -> None:
    """Carry only the entry records health needs, avoiding an O(S) checkout."""
    additions = _addition_rows(delta)
    destination.mkdir(parents=True, exist_ok=True)
    for target in sorted(set(targets)):
        if target in additions:
            raw = _record(entries / f"{target}.json", target)
            if not _matches_row(raw, additions[target]):
                raise ValueError(f"entry record {target} disagrees with the release delta")
        elif target in canonical_targets:
            if database is None:
                raise ValueError(
                    f"historical publication health target {target} needs the canonical Git checkout"
                )
            raw = _committed_record(database, commit, target)
            raw = _record_bytes(raw, target, f"entries/{target}.json at {commit}")
        else:
            raw = _record(entries / f"{target}.json", target)
        (destination / f"{target}.json").write_bytes(raw)


def _verify_bundled_records(
    delta: dict[str, Any], entries: pathlib.Path, targets: list[str],
    *, database: pathlib.Path | None, commit: str, canonical_targets: set[str],
) -> None:
    expected = sorted(f"{target}.json" for target in set(targets))
    if not expected and not entries.exists():
        return
    if entries.is_symlink() or not entries.is_dir():
        raise ValueError("publication evidence entry-record directory is missing or symbolic")
    children = list(entries.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in children):
        raise ValueError("publication evidence entry-record directory contains a non-file")
    if sorted(path.name for path in children) != expected:
        raise ValueError("publication evidence carries the wrong set of entry records")

    additions = _addition_rows(delta)
    for target in sorted(set(targets)):
        raw = _record(entries / f"{target}.json", target)
        if target in additions:
            if not _matches_row(raw, additions[target]):
                raise ValueError(f"bundled entry record {target} disagrees with the release delta")
        elif target in canonical_targets:
            if database is None:
                raise ValueError(
                    f"historical publication health target {target} needs the canonical Git checkout"
                )
            if raw != _committed_record(database, commit, target):
                raise ValueError(
                    f"bundled historical entry record {target} disagrees with canonical Git"
                )


def prepare(
    delta_path: pathlib.Path,
    *,
    previous_base: pathlib.Path | None = None,
    database: pathlib.Path | None = None,
    commit: str,
    entries: pathlib.Path,
    bundle_entries: pathlib.Path,
) -> None:
    """Validate the activated delta and bundle its proportional health inputs."""
    expected_commit = _commit(commit)
    delta = _read_delta(delta_path)
    if delta["database_commit"] != expected_commit:
        raise ValueError("release delta belongs to a different publishing commit")
    if delta["parent"] is not None:
        if previous_base is None:
            raise ValueError("incremental publication health needs its served parent base")
        raw = previous_base.read_bytes()
        previous = release_delta.parse_base(raw)
        if raw != release_delta.canonical_base_bytes(previous):
            raise ValueError("publication-base.json is not in canonical byte form")
        if delta["parent"] != previous["release"]:
            raise ValueError("incremental release does not name the supplied served parent")
        if delta["takedowns_git_blob"] != previous["takedowns_git_blob"]:
            raise ValueError(
                "incremental release takedown authority differs from its served parent"
            )
        if database is None:
            raise ValueError("incremental publication health needs the canonical Git checkout")
        current_blob = committed_manifest_blob(database, "HEAD")
        parent_blob = committed_manifest_blob(database, previous["database_commit"])
        if current_blob != parent_blob or current_blob != delta["takedowns_git_blob"]:
            raise ValueError(
                "incremental release takedown authority disagrees with canonical Git"
            )
    versions, withdrawn = versions_in(delta)
    historical = set(versions) - set(_addition_rows(delta))
    _bundle_records(
        delta,
        entries,
        bundle_entries,
        versions + withdrawn,
        database=database,
        commit=expected_commit,
        canonical_targets=historical,
    )


def verify(
    delta_path: pathlib.Path,
    *,
    commit: str,
    entries: pathlib.Path,
    database: pathlib.Path | None = None,
) -> tuple[list[str], list[str]]:
    """Validate the run-selected delta before using any path from it."""
    expected_commit = _commit(commit)
    delta = _read_delta(delta_path)
    if delta["database_commit"] != expected_commit:
        raise ValueError("release delta belongs to a different triggering commit")
    versions, withdrawn = versions_in(delta)
    historical = set(versions) - set(_addition_rows(delta))
    _verify_bundled_records(
        delta,
        entries,
        versions + withdrawn,
        database=database,
        commit=expected_commit,
        canonical_targets=historical,
    )
    return versions, withdrawn


def choose_mode(
    event_name: str,
    publication_conclusion: str,
    download_outcome: str,
    evidence_outcome: str,
    publication_event: str,
    triggering_actor: str,
) -> str:
    """Only valid run-selected evidence from a successful publication earns delta mode."""
    if (
        event_name == "workflow_run"
        and publication_conclusion == "success"
        and download_outcome == "success"
        and evidence_outcome == "success"
        and not (
            publication_event == "workflow_dispatch"
            and triggering_actor == "github-actions[bot]"
        )
    ):
        return "delta"
    return "full"


def _write_lines(path: pathlib.Path, values: list[str]) -> None:
    path.write_text("".join(f"{value}\n" for value in values), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    preparing = subparsers.add_parser("prepare")
    preparing.add_argument("--delta", type=pathlib.Path, required=True)
    preparing.add_argument(
        "--previous-base",
        type=pathlib.Path,
        help="the authenticated served publication base; required for an incremental release",
    )
    preparing.add_argument(
        "--database",
        type=pathlib.Path,
        help="canonical Git checkout; required to bind an incremental takedown authority",
    )
    preparing.add_argument("--commit", required=True)
    preparing.add_argument("--entries", type=pathlib.Path, required=True)
    preparing.add_argument("--bundle-entries", type=pathlib.Path, required=True)

    verifying = subparsers.add_parser("verify")
    verifying.add_argument("--delta", type=pathlib.Path, required=True)
    verifying.add_argument("--commit", required=True)
    verifying.add_argument("--entries", type=pathlib.Path, required=True)
    verifying.add_argument(
        "--database",
        type=pathlib.Path,
        help="canonical Git checkout used to bind historical entry records",
    )
    verifying.add_argument("--versions", type=pathlib.Path, required=True)
    verifying.add_argument("--withdrawn", type=pathlib.Path, required=True)

    choosing = subparsers.add_parser("choose-mode")
    choosing.add_argument("--event-name", required=True)
    choosing.add_argument("--publication-conclusion", default="")
    choosing.add_argument("--download-outcome", default="")
    choosing.add_argument("--evidence-outcome", default="")
    choosing.add_argument("--publication-event", default="")
    choosing.add_argument("--triggering-actor", default="")
    choosing.add_argument("--github-output", type=pathlib.Path, required=True)

    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare(
                args.delta,
                previous_base=args.previous_base,
                database=args.database,
                commit=args.commit,
                entries=args.entries,
                bundle_entries=args.bundle_entries,
            )
            print(f"prepared publication health evidence for commit {args.commit}")
            return 0
        if args.command == "verify":
            versions, withdrawn = verify(
                args.delta,
                commit=args.commit,
                entries=args.entries,
                database=args.database,
            )
            _write_lines(args.versions, versions)
            _write_lines(args.withdrawn, withdrawn)
            print(
                f"validated health evidence for commit {args.commit}: "
                f"{len(versions)} added version(s), {len(withdrawn)} withdrawal(s)"
            )
            return 0
        mode = choose_mode(
            args.event_name,
            args.publication_conclusion,
            args.download_outcome,
            args.evidence_outcome,
            args.publication_event,
            args.triggering_actor,
        )
        with args.github_output.open("a", encoding="utf-8") as stream:
            stream.write(f"mode={mode}\n")
        print(f"post-publication health mode: {mode}")
        return 0
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        print(f"publication evidence is unusable: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
