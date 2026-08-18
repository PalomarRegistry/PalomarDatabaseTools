#!/usr/bin/env python3
"""What one release adds to the one before it.

A release used to be described by a manifest naming every object in the
dataset. That is O(S) to build, to hash, to upload and to read back, on a
publication that happens once per accepted result -- so the registry paid O(S²)
over its life for a document that is almost entirely a copy of the last one.

A delta names what changed. The set of published records is carried instead as
a count and a `root`, an additive multiset hash over the canonical
`(path, bytes, sha256)` tuple of every immutable object. Addition and removal
are addition and subtraction modulo 2^256, so the publisher maintains it in
O(changed) from the parent's root -- and cannot maintain it without declaring
exactly what it added and took away. A stager that silently dropped a record
would have to declare a withdrawal to keep the arithmetic, and a stager that
substituted one record for another would keep the count and lose the root.

The construction is MSet-Add-Hash: set-collision-resistant when the summands
are hashes of the elements. A bare XOR would also be incremental and is what
one reaches for first; it is linear over GF(2), so subsets can be made to
cancel, and the sum is used instead for that reason.

A deliberately full release still has a full delta. The next ordinary event
does not download it: pointer schema 3 selects a hash-authenticated,
constant-size `publication-base.json` projection carrying only the parent
release, commit, surface layout, record count/root and takedown Git blob.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
from typing import Any, Iterable, Mapping, Sequence

DELTA_PATH = "release-delta.json"
DELTA_SCHEMA = 6
BASE_PATH = "publication-base.json"
BASE_SCHEMA = 1
# Which layout of the derived surfaces the release was built in. A release can
# only be described as a difference from its parent while both agree about what
# objects exist, so a change to the shape of browsing, the subject pages or the
# version indexes bumps this and the next release is a full one. Nothing else
# would notice: the surfaces the new layout does not write would be left in the
# bucket, served, and stale, and an incremental release deliberately never
# lists the bucket to find out. 1 was a hundred fixed browse shards and one
# object per classification code; 2 is paging by the day a result was
# registered; 3 makes the landing projection self-contained; 4 adds the
# bounded repository and exact registration-identity lookups used by intake;
# 5 adds the bounded active source-commit set to each exact identity lookup;
# 6 labels the registered-status version-index contract as schema version 2.
# Unchanged rows cannot be upgraded by an incremental release, because that
# release has only the records it touched, so the first release in any new
# layout rebuilds it.
SURFACES = 6
MODULUS = 1 << 256


def tuple_digest(path: str, size: int, sha256: str) -> int:
    """One object's contribution to the root.

    The fields are length-prefixed rather than joined by a separator, so that
    no two different objects can produce the same preimage. A bare
    concatenation would let a path ending in a digit and a size beginning with
    one trade places.
    """
    parts = (path.encode("utf-8"), str(int(size)).encode(), sha256.encode())
    preimage = b"".join(len(part).to_bytes(8, "big") + part for part in parts)
    return int.from_bytes(hashlib.sha256(preimage).digest(), "big")


def root_of(rows: Iterable[Mapping[str, Any]]) -> str:
    """The root of a whole set, for a full rebuild or an audit."""
    total = 0
    for row in rows:
        total = (total + tuple_digest(row["path"], row["bytes"], row["sha256"])) % MODULUS
    return f"{total:064x}"


def root_after(parent_root: str, added: Iterable[Mapping[str, Any]],
               removed: Iterable[Mapping[str, Any]]) -> str:
    """The same root, reached in O(changed) instead of O(everything)."""
    total = int(parent_root, 16)
    for row in added:
        total = (total + tuple_digest(row["path"], row["bytes"], row["sha256"])) % MODULUS
    for row in removed:
        total = (total - tuple_digest(row["path"], row["bytes"], row["sha256"])) % MODULUS
    return f"{total:064x}"


EMPTY_ROOT = f"{0:064x}"


def release_id(delta: Mapping[str, Any]) -> str:
    """A release is named by the digest of the document that describes it.

    Self-authenticating, exactly as the manifest was: given the pointer, the
    delta cannot be swapped for another without the pointer changing too.
    """
    return hashlib.sha256(canonical_bytes(delta)).hexdigest()


def base_of(delta: Mapping[str, Any]) -> dict[str, Any]:
    """Constant-size authenticated inputs needed by the next accepted event."""
    return {
        "schema_version": BASE_SCHEMA,
        "release": release_id(delta),
        "database_commit": delta["database_commit"],
        "surfaces": delta["surfaces"],
        "takedowns_git_blob": delta["takedowns_git_blob"],
        "records": dict(delta["records"]),
    }


def canonical_base_bytes(base: Mapping[str, Any]) -> bytes:
    return json.dumps(base, indent=2, sort_keys=True).encode("utf-8") + b"\n"


def base_id(base: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_base_bytes(base)).hexdigest()


def canonical_bytes(delta: Mapping[str, Any]) -> bytes:
    return json.dumps(delta, indent=2, sort_keys=True).encode("utf-8") + b"\n"


ROW_KEYS = {"path", "bytes", "sha256"}


def _rows(value: object, where: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(f"{DELTA_PATH}: {where} must be an array")
    previous = ""
    rows: list[dict[str, Any]] = []
    for position, row in enumerate(value):
        if not isinstance(row, dict) or set(row) != ROW_KEYS:
            raise ValueError(f"{DELTA_PATH}: {where}[{position}] is malformed")
        path = row["path"]
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or "\\" in path
            or ".." in path.split("/")
            or path <= previous
        ):
            raise ValueError(f"{DELTA_PATH}: {where} path is unsafe or unsorted: {path!r}")
        if not isinstance(row["bytes"], int) or isinstance(row["bytes"], bool) or row["bytes"] < 0:
            raise ValueError(f"{DELTA_PATH}: {where}[{position}] has an invalid size")
        if not isinstance(row["sha256"], str) or len(row["sha256"]) != 64:
            raise ValueError(f"{DELTA_PATH}: {where}[{position}] has an invalid digest")
        previous = path
        rows.append(dict(row))
    return rows


def _paths(value: object, where: str) -> list[str]:
    """Keys with no digest, because they name what is going rather than what is.

    `withdrawals` carries rows, since a record's digest is what lets the root be
    maintained by subtraction. A derived object contributes to no root, so there
    is nothing here to subtract and nothing to compare a digest against: the
    object is going, and its bytes are not the publisher's business.
    """
    if not isinstance(value, list):
        raise ValueError(f"{DELTA_PATH}: {where} must be an array")
    previous = ""
    for path in value:
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or "\\" in path
            or ".." in path.split("/")
            or path <= previous
        ):
            raise ValueError(f"{DELTA_PATH}: {where} path is unsafe or unsorted: {path!r}")
        previous = path
    return list(value)


def parse(raw: bytes) -> dict[str, Any]:
    """Read a delta, refusing anything this tool would have to guess about."""
    delta = json.loads(raw)
    expected = {
        "schema_version", "surfaces", "parent", "database_commit",
        "additions", "withdrawals", "retired", "stable", "aggregates",
        "takedowns_git_blob", "records",
    }
    if not isinstance(delta, dict) or set(delta) != expected:
        raise ValueError(f"{DELTA_PATH} has an invalid top-level shape")
    if delta["schema_version"] != DELTA_SCHEMA:
        raise ValueError(f"{DELTA_PATH} has an unsupported schema")
    if not isinstance(delta["surfaces"], int) or isinstance(delta["surfaces"], bool):
        raise ValueError(f"{DELTA_PATH}: surfaces must be a layout number")
    parent = delta["parent"]
    if parent is not None and (not isinstance(parent, str) or len(parent) != 64):
        raise ValueError(f"{DELTA_PATH}: parent must be a release id or null")
    commit = delta["database_commit"]
    if not isinstance(commit, str) or len(commit) != 40:
        raise ValueError(f"{DELTA_PATH}: database_commit must be a commit id")
    # Withdrawals carry the same rows as additions, and not merely the
    # identifiers they belong to. The digests are what let the root be
    # maintained by subtraction, and the paths are what the publisher deletes:
    # asking it to work either out for itself would mean listing a prefix to
    # find out what it is taking away.
    for key in ("additions", "withdrawals", "stable", "aggregates"):
        delta[key] = _rows(delta[key], key)
    # Derived objects an incremental release takes away. A stable key normally
    # only ever changes, so until a word's postings could shrink there was
    # nothing here to name: a browse page a withdrawal emptied stays, holding
    # nothing, because that is the answer a rebuild gives too. A word's head is
    # not like that. It exists only while some result carries the word, so
    # leaving one behind saying it has no results would publish the fact that
    # some withdrawn record carried that word.
    delta["retired"] = _paths(delta["retired"], "retired")
    takedowns_blob = delta["takedowns_git_blob"]
    if (
        not isinstance(takedowns_blob, str)
        or re.fullmatch(r"[0-9a-f]{40}", takedowns_blob) is None
    ):
        raise ValueError(
            f"{DELTA_PATH}: takedowns_git_blob must be a lowercase Git blob id"
        )
    records = delta["records"]
    if not isinstance(records, dict) or set(records) != {"count", "root"}:
        raise ValueError(f"{DELTA_PATH}: records must carry a count and a root")
    if not isinstance(records["count"], int) or records["count"] < 0:
        raise ValueError(f"{DELTA_PATH}: records.count must be a count")
    if not isinstance(records["root"], str) or len(records["root"]) != 64:
        raise ValueError(f"{DELTA_PATH}: records.root must be a digest")
    return delta


def parse_base(raw: bytes) -> dict[str, Any]:
    """Read the closed constant-size projection selected by the R2 pointer."""
    base = json.loads(raw)
    expected = {
        "schema_version", "release", "database_commit", "surfaces",
        "takedowns_git_blob", "records",
    }
    if not isinstance(base, dict) or set(base) != expected:
        raise ValueError(f"{BASE_PATH} has an invalid top-level shape")
    if type(base["schema_version"]) is not int or base["schema_version"] != BASE_SCHEMA:
        raise ValueError(f"{BASE_PATH} has an unsupported schema")
    for key, size in (("release", 64), ("database_commit", 40), ("takedowns_git_blob", 40)):
        value = base[key]
        if not isinstance(value, str) or re.fullmatch(rf"[0-9a-f]{{{size}}}", value) is None:
            raise ValueError(f"{BASE_PATH}: {key} must be a lowercase object id")
    if type(base["surfaces"]) is not int:
        raise ValueError(f"{BASE_PATH}: surfaces must be a layout number")
    records = base["records"]
    if not isinstance(records, dict) or set(records) != {"count", "root"}:
        raise ValueError(f"{BASE_PATH}: records must carry a count and a root")
    if type(records["count"]) is not int or records["count"] < 0:
        raise ValueError(f"{BASE_PATH}: records.count must be a count")
    if (
        not isinstance(records["root"], str)
        or re.fullmatch(r"[0-9a-f]{64}", records["root"]) is None
    ):
        raise ValueError(f"{BASE_PATH}: records.root must be a digest")
    return base


def database_commit(path: pathlib.Path) -> str:
    """The checkout commit named by one canonical publication base."""
    return str(parse_base(path.read_bytes())["database_commit"])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-commit",
        type=pathlib.Path,
        help="print the database commit from this publication base",
    )
    args = parser.parse_args(argv)
    if args.database_commit is None:
        parser.error("--database-commit is required")
    try:
        print(database_commit(args.database_commit))
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"cannot use the served release as a validation base: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
