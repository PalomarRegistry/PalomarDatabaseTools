#!/usr/bin/env python3
"""Publish a staged public snapshot to a private R2 bucket.

An object is keyed by how it changes rather than by when it was published.
Records that the append-only invariant already freezes -- entries, renders and
evidence -- sit at a stable key under `public/` and are written once, ever.
Source availability is self-consistent and is rewritten in place by its own
publisher. Derived browse, feed, search, subject, tombstone and version
documents are also stable keys rewritten only when touched. The small release
aggregates keep the release prefix and pointer flip, so a reader still sees one
consistent schema and licence set.

Keying every object by the release, as this once did, meant that adding one
entry rewrote the entire dataset. That is quadratic in the number of entries:
at ten thousand entries it is over a billion object writes.

The cost of stable keys is that the pointer no longer makes a withdrawal
atomic. A withdrawn record has to be deleted, so the delete is verified and the
pointer is moved only afterwards. The failure mode is then a publication that
stops having changed nothing, never an index that says a record is gone while
the record is still being served.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import pathlib
import re
from typing import Any

POINTER_KEY = "_current.json"
BUCKET_DEFAULT = "palomar-public-data"
PUBLIC_PREFIX = "public/"
SNAPSHOT_PREFIX = "snapshots/"
# Neither served nor retained: a scratch key the publisher uses to establish
# that this origin still refuses a conditional write. See
# `_assert_conditional_writes`.
PROBE_PREFIX = "internal/"
AVAILABILITY_PATH = "source-availability.json"
AVAILABILITY_TARGETS_PATH = "source-availability-targets.json"
# Rewritten in place at a key that does not depend on the release. A feed is a
# notification channel and is allowed to run ahead of the rest of the release;
# that is what an RSS reader assumes anyway, and it is what lets one new entry
# rewrite the seven feeds it belongs to rather than all of them under a fresh
# release prefix. `recent.json` is the same bargain for the landing page, and
# `recent-renders.json` accompanies it and is rewritten with it.
STABLE_PATHS = (
    AVAILABILITY_PATH,
    AVAILABILITY_TARGETS_PATH,
    "feed.xml",
    "recent.json",
    "recent-renders.json",
)
STABLE_PREFIXES = (
    "browse/", "feeds/", "registration-identities/", "repositories/", "search/",
    "subjects/", "tombstones/", "versions/"
)
# Written independently and never put in a snapshot staging tree: the manifest
# by the Worker's `_operations/source-availability` endpoint, the target set by
# `publish_availability_targets.py`. This publisher must not withdraw either
# merely because its own stager did not produce it: that is the difference
# between a path it owns and a path it only shares a bucket with.
UNOWNED_PATHS = (AVAILABILITY_PATH, AVAILABILITY_TARGETS_PATH)
# The stable documents every release writes, whatever the registry holds. A
# release that named one of these has not retired it, it has failed to write
# it, and the difference is a landing page that has forgotten the registry.
# Everything else at a stable key is per-record or per-word and may genuinely
# stop existing.
ALWAYS_WRITTEN = (
    AVAILABILITY_PATH,
    "feed.xml",
    "recent.json",
    "recent-renders.json",
    "browse/index.json",
)
LAUNCH_MARKER = ".palomar-launched"
import release_delta

MANIFEST_PATH = release_delta.DELTA_PATH
IMMUTABLE_PREFIXES = ("entries/", "renders/", "evidence/")
POINTER_SCHEMA = 3
VERSIONED = r"PALOMAR-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}-v[1-9][0-9]*"
RECORD_RE = re.compile(rf"^(?:entries|renders|evidence)/({VERSIONED})(?:\.json$|/)")
TOMBSTONE_RE = re.compile(rf"^tombstones/({VERSIONED})\.json$")
# The stable paths a release may write, as the Worker's own grammar has them.
# `--audit` can no longer ask whether the current delta names an object, since
# a delta names only what its release wrote, so it asks whether the origin is
# holding something nothing could ever serve.
DAY_PAGES = r"(?:[0-9]{4}\.json|[0-9]{4}-[0-9]{2}-[0-9]{2}/[1-9][0-9]*\.json)"
CODE = r"(?:arxiv|msc)/[A-Za-z0-9.-]+"
TERM = r"[a-z0-9]{2,32}"
SERVED_RE = re.compile(
    rf"^(?:feed\.xml|recent\.json"
    rf"|versions/PALOMAR-[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}-[0-9]{{6}}\.json"
    rf"|registration-identities/[0-9a-f]{{64}}\.json"
    rf"|repositories/[a-z0-9_.-]+/[a-z0-9_.-]+\.json"
    rf"|browse/(?:index\.json|{DAY_PAGES})"
    rf"|feeds/{CODE}\.xml"
    rf"|subjects/{CODE}\.json"
    rf"|subjects/{CODE}/{DAY_PAGES}"
    rf"|tombstones/{VERSIONED}\.json"
    rf"|search/stopwords\.json"
    rf"|search/t/{TERM}/(?:head|[0-9]{{1,4}})\.json)$"
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _content_type(path: str) -> str:
    if path.endswith(".json"):
        return "application/json; charset=utf-8"
    if path.endswith(".xml"):
        return "application/rss+xml; charset=utf-8"
    if path == "LICENSE" or path.endswith((".md", ".txt")):
        return "text/plain; charset=utf-8"
    guessed, _encoding = mimetypes.guess_type(path)
    if guessed is None:
        return "application/octet-stream"
    if guessed.startswith("text/") or guessed in {"application/javascript"}:
        return f"{guessed}; charset=utf-8"
    return guessed


def classify(relative: str) -> str:
    """Which of the three kinds of object a public path is.

    `immutable` is written once at a stable key, because the append-only
    invariant already freezes those bytes. `stable` also sits at a stable key
    but is rewritten in place, and is therefore allowed to be briefly ahead of
    or behind the release. `snapshot` is everything the pointer has to make
    consistent, and keeps the release prefix.

    Anything unrecognised is deliberately a snapshot path. A path nobody has
    classified then becomes release-keyed, which is only more expensive; calling
    it immutable would silently freeze a file that changes. The Worker makes the
    same decision, and both are read against tests/path-classes.json.
    """
    path = relative[1:] if relative.startswith("/") else relative
    if path in STABLE_PATHS or path.startswith(STABLE_PREFIXES):
        return "stable"
    return "immutable" if path.startswith(IMMUTABLE_PREFIXES) else "snapshot"


YEAR_FILE_RE = re.compile(r"[0-9]{4}\.json")


def _ordering_path(path: str) -> str:
    """The release-relative namespace used to order one staged or stored path.

    Writes arrive as release-relative paths; deletes arrive as complete object
    keys. Strip exactly the canonical public prefix once. Near-prefixes and
    malformed keys remain unrecognised rather than being reinterpreted as a
    served hierarchy.
    """
    return path[len(PUBLIC_PREFIX) :] if path.startswith(PUBLIC_PREFIX) else path


def _rank(path: str) -> int:
    """The dependency rank of a page, year document, or head.

    A reader takes a head as its account of which years are worth fetching, and
    a year document as its account of which days and pages are, so everything a
    document names has to be at least as new as the document naming it. That is
    the same reason the release delta is written last: a head written first
    could say a day held nothing while the record that filled it was already
    being served.

    Writes pass a release-relative path and stable-key deletes pass a full
    `public/` object key. Normalize that namespace once before reading the
    shape of the path rather than teaching each surface about both forms. A
    classification code cannot be four digits under either vocabulary, so a
    code's front page is never mistaken for a year document.

    This is an ordering rank, not a filename taxonomy. A numeric search page
    such as `search/t/ring/2026.json` resembles a year document but names
    nothing, so only the two actual year-document shapes receive that rank.
    `_delete_keys` also receives unreachable old `snapshots/` keys after a
    pointer flip; none matches a served hierarchy rooted at its first component,
    so those stay together at the default rank.
    """
    parts = _ordering_path(path).split("/")
    if YEAR_FILE_RE.fullmatch(parts[-1]) and (
        (parts[0] == "browse" and len(parts) == 2)
        or (parts[0] == "subjects" and len(parts) == 4)
    ):
        return 1
    # A postings head is the same kind of thing: it is a word's only account
    # of how many pages there are.
    if (
        parts == ["browse", "index.json"]
        or (parts[0] == "subjects" and len(parts) == 3)
        or (
            parts[0:2] == ["search", "t"]
            and len(parts) == 4
            and parts[-1] == "head.json"
        )
    ):
        return 2
    return 0


def object_key(relative: str, release: str) -> str:
    if classify(relative) == "snapshot":
        return f"{SNAPSHOT_PREFIX}{release}/{relative}"
    return f"{PUBLIC_PREFIX}{relative}"


def _error_code(error: Exception) -> str:
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return ""
    detail = response.get("Error")
    return str(detail.get("Code")) if isinstance(detail, dict) else ""


def _already_there(error: Exception) -> bool:
    """Whether a conditional put failed because the key was already taken.

    R2 answers a failed `If-None-Match: *` with 412. Botocore surfaces the
    status as the error code for responses it has no modelled name for, so
    both spellings are accepted rather than pinning one and silently treating
    a precondition failure as a transport error.
    """
    return _error_code(error) in {"412", "PreconditionFailed"}


def _not_found(error: Exception) -> bool:
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return False
    detail = response.get("Error")
    return isinstance(detail, dict) and str(detail.get("Code")) in {
        "404",
        "NoSuchKey",
        "NotFound",
    }


def _head(client: Any, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        return client.head_object(Bucket=bucket, Key=key)
    except Exception as error:
        if _not_found(error):
            return None
        raise


def _read_object(client: Any, bucket: str, key: str) -> bytes | None:
    if _head(client, bucket, key) is None:
        return None
    response = client.get_object(Bucket=bucket, Key=key)
    body = response["Body"]
    try:
        return body.read()
    finally:
        close = getattr(body, "close", None)
        if close:
            close()


def _read_pointer(client: Any, bucket: str) -> tuple[str, int, str] | None:
    """The release and constant-size base projection the origin selects."""
    raw = _read_object(client, bucket, POINTER_KEY)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    if set(value) != {"schema_version", "release", "publication_base"}:
        return None
    release = value["release"]
    schema = value["schema_version"]
    publication_base = value["publication_base"]
    if (
        not isinstance(release, str)
        or re.fullmatch(r"[0-9a-f]{64}", release) is None
        or type(schema) is not int
        or not isinstance(publication_base, str)
        or re.fullmatch(r"[0-9a-f]{64}", publication_base) is None
    ):
        return None
    return release, schema, publication_base


def _staged_objects(site: pathlib.Path) -> tuple[str, dict[str, Any], list[tuple[str, bytes, str, str]]]:
    """Read exactly the objects the delta says this release writes.

    This used to walk the whole staged tree and re-hash every byte in it, once
    per publication, on a release that adds one record. The delta names what
    changed, so an incremental release opens a handful of files whatever the
    registry weighs; a full rebuild names everything and still costs O(S),
    which is what a full rebuild is.
    """
    delta_bytes = (site / MANIFEST_PATH).read_bytes()
    delta = release_delta.parse(delta_bytes)
    objects: list[tuple[str, bytes, str, str]] = []
    expected_class = {
        "additions": "immutable",
        "stable": "stable",
        "aggregates": "snapshot",
    }
    for group in ("additions", "stable", "aggregates"):
        for row in delta[group]:
            relative = row["path"]
            if relative in UNOWNED_PATHS:
                # Published on its own schedule and by another tool, so it must
                # not be able to change the release id; the stager leaves it
                # out. Feeds are stable-keyed too but *are* owned here and do
                # belong: a feed changes exactly when the dataset does, which
                # is what a new release id is supposed to mean.
                raise ValueError(f"the release delta must not contain {relative}")
            path = site / relative
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"release file is missing or symbolic: {relative}")
            if path.stat().st_size != row["bytes"]:
                raise ValueError(f"release file disagrees with the delta: {relative}")
            kind = classify(relative)
            if kind != expected_class[group]:
                raise ValueError(
                    f"release delta classifies {relative} as {group}, not {kind}"
                )
            objects.append((relative, row["sha256"], kind))
    return release_delta.release_id(delta), delta, objects


def _staged_bytes(site: pathlib.Path, relative: str, digest: str) -> bytes:
    """Read one staged object, and prove it is the one the delta describes.

    Held for one write and one readback rather than for the whole publication.
    Carrying every staged byte at once put a 7 GB runner within reach of a
    dataset it could otherwise publish, and the hashing is the same work either
    way -- it is only the lifetime that was wrong.
    """
    data = (site / relative).read_bytes()
    if _sha256(data) != digest:
        raise ValueError(f"release file disagrees with the delta: {relative}")
    return data


def _list_keys(client: Any, bucket: str, prefix: str) -> dict[str, int]:
    """Every key under a prefix, with its size. Strongly consistent in R2."""
    keys: dict[str, int] = {}
    token: str | None = None
    while True:
        arguments: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if token:
            arguments["ContinuationToken"] = token
        response = client.list_objects_v2(**arguments)
        for item in response.get("Contents", []):
            key = item.get("Key")
            if isinstance(key, str):
                keys[key] = int(item.get("Size", -1))
        if not response.get("IsTruncated"):
            return keys
        token = response.get("NextContinuationToken")
        if not isinstance(token, str):
            raise RuntimeError(f"R2 listing of {prefix} was truncated without a continuation token")


def read_delta(client: Any, bucket: str, release: str) -> dict[str, Any] | None:
    """One release's own account of itself, authenticated by its name.

    A release is named by the digest of this document, so a delta that does not
    hash to the id it was fetched under is not that release, and is refused
    rather than believed.
    """
    raw = _read_object(client, bucket, f"{SNAPSHOT_PREFIX}{release}/{MANIFEST_PATH}")
    if raw is None:
        return None
    try:
        delta = release_delta.parse(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        print(f"the release delta for {release[:12]} is unreadable: {error}")
        return None
    if release_delta.release_id(delta) != release:
        raise RuntimeError(
            f"the delta stored under {release} is not the delta that names it; "
            "refusing to treat it as an authority for anything"
        )
    return delta


def _publication_base(
    client: Any, bucket: str, pointer: tuple[str, int, str] | None
) -> dict[str, Any] | None:
    """Authenticated constant-size parent state, without opening its full delta."""
    if pointer is None or pointer[1] != POINTER_SCHEMA:
        return None
    raw = _read_object(
        client,
        bucket,
        f"{SNAPSHOT_PREFIX}{pointer[0]}/{release_delta.BASE_PATH}",
    )
    if raw is None:
        return None
    if _sha256(raw) != pointer[2]:
        raise RuntimeError("the publication base does not match the R2 pointer")
    try:
        base = release_delta.parse_base(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise RuntimeError(f"the publication base is unreadable: {error}") from error
    if raw != release_delta.canonical_base_bytes(base):
        raise RuntimeError("the publication base is not in canonical byte form")
    if base["release"] != pointer[0]:
        raise RuntimeError("the publication base names a different release than the pointer")
    return base


def _put_object(
    client: Any,
    bucket: str,
    key: str,
    data: bytes,
    digest: str,
    path: str,
    *,
    create_only: bool = False,
) -> bool:
    """Write one object. Returns whether this call is what created it.

    `create_only` is for the objects the append-only invariant freezes. The
    publisher already refused to write different bytes at a path the previous
    release attested, but that was a check the publisher performed on itself:
    a publisher that lost the previous manifest, or read the wrong one,
    overwrote instead. `If-None-Match: *` moves the refusal into R2, where no
    bug on this side can talk it round.

    An existing key is not an error here. A publication that crashed after
    writing an object but before moving the pointer leaves exactly that state,
    and its retry has to be able to finish; the caller checks the bytes.
    """
    arguments: dict[str, Any] = {
        "Bucket": bucket,
        "Key": key,
        "Body": data,
        "ContentLength": len(data),
        "ContentType": _content_type(path),
        "Metadata": {"sha256": digest},
    }
    if create_only:
        arguments["IfNoneMatch"] = "*"
    try:
        client.put_object(**arguments)
    except Exception as error:
        if create_only and _already_there(error):
            return False
        raise
    return True


def _assert_conditional_writes(client: Any, bucket: str) -> None:
    """Establish that the origin actually refuses a write to a taken key.

    Everything above rests on R2 honouring `If-None-Match: *`. An origin that
    ignored an unrecognised header would accept every conditional write, the
    publisher would carry on, and the guarantee in `docs/append-only.md` would
    simply be untrue -- with nothing failing to say so. That is the shape of
    bug this repository keeps finding, so it is checked rather than assumed.

    Two operations per publication, against a key nothing serves. If the
    origin cannot give us create-only, publication stops: the alternative is
    to keep writing while believing a guarantee that is not there.
    """
    key = f"{PROBE_PREFIX}conditional-write"
    body = b"probe\n"
    digest = _sha256(body)
    _put_object(client, bucket, key, body, digest, "probe.txt")
    if _put_object(client, bucket, key, b"different\n", _sha256(b"different\n"), "probe.txt",
                   create_only=True):
        raise RuntimeError(
            "this origin accepted a conditional write to a key that already exists, so "
            "published records are not protected from being overwritten. Refusing to "
            "publish while the append-only guarantee cannot be enforced at the origin."
        )
    if _read_object(client, bucket, key) != body:
        raise RuntimeError("the conditional-write probe was overwritten despite being refused")


def _verify(client: Any, bucket: str, key: str, expected: bytes, digest: str) -> None:
    response = client.get_object(Bucket=bucket, Key=key)
    body = response["Body"]
    try:
        actual = body.read()
    finally:
        close = getattr(body, "close", None)
        if close:
            close()
    metadata = response.get("Metadata") or {}
    if actual != expected or _sha256(actual) != digest or metadata.get("sha256") != digest:
        raise RuntimeError(f"R2 readback verification failed: {key}")


def _delete_keys(client: Any, bucket: str, keys: list[str]) -> None:
    """Delete, and insist that it happened.

    A batch delete answers 200 with a per-object error array, so the response is
    the only place a refusal appears. Discarding it would leave a withdrawn
    record served for ever while the index said it was gone. This handles both
    served `public/` keys before the pointer moves and old `snapshots/` keys
    after it; only the former have reader-visible dependencies.
    """
    ranked: dict[int, list[str]] = {}
    for key in keys:
        ranked.setdefault(_rank(key), []).append(key)

    # This reverses the dependency ranks used by writes, not the lexical order
    # inside a rank. A head must disappear before the year documents and pages
    # it names. Separate calls make that ordering real at R2: if a head batch
    # is refused, no call can already have deleted a lower-rank dependency.
    for rank in sorted(ranked, reverse=True):
        ordered = sorted(ranked[rank])
        for start in range(0, len(ordered), 1000):
            batch = ordered[start : start + 1000]
            response = client.delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
            )
            problems = [
                f"{failure.get('Key')}: {failure.get('Code')} {failure.get('Message')}"
                for failure in (response or {}).get("Errors") or []
            ]
            if problems:
                raise RuntimeError("R2 refused to delete:\n" + "\n".join(problems))

        # Confirm each dependency rank before moving down to objects it names.
        # R2's listing is strongly consistent, so absence from a fresh listing
        # is as strong as a 404 without one request per key.
        prefixes = sorted({key.rsplit("/", 1)[0] + "/" for key in ordered})
        surviving: set[str] = set()
        for prefix in prefixes:
            surviving |= set(_list_keys(client, bucket, prefix))
        still_there = sorted(set(ordered) & surviving)
        if still_there:
            raise RuntimeError(
                "R2 reported a delete that did not happen:\n" + "\n".join(still_there)
            )


def _launched(database: pathlib.Path) -> bool:
    """Whether this database has published anything it has promised to keep.

    The withdrawal guard refuses to unpublish a record without a tombstone,
    because a record leaving the staged set means either a takedown or a broken
    stager. Before launch it means neither: a pre-launch database is
    deliberately reshapeable and its records are not publication history yet.
    `check_append_only` reads the same marker for the same reason.

    The database is identified rather than assumed, so pointing this at the
    wrong directory fails loudly instead of quietly disarming the guard.
    """
    if not all((database / name).is_file() for name in ("schema-v3.json", "scores-v1.json")):
        raise RuntimeError(
            f"{database} does not look like the canonical database: no "
            "entry and score schema pair. "
            "Refusing to decide whether the withdrawal guard applies."
        )
    return (database / LAUNCH_MARKER).exists()


def _removals(existing: dict[str, int], staged_keys: set[str], tombstoned: set[str]) -> list[str]:
    """Which stable keys this publication takes away, and why it may.

    A *record* leaves the staged set for exactly two reasons: it was taken
    down, or the stager is broken. The tombstone is what tells them apart, and
    the stager writes one for every takedown, so the guard is satisfied by a
    real withdrawal and fatal for a bug.

    A feed needs no such justification: it is derived, and the code it covers
    genuinely stops existing when the last entry carrying it is superseded or
    withdrawn. Deleting it is how a category stops answering 200 with nothing
    in it. `source-availability.json` is exempt entirely, because this
    publisher does not own it and never stages it.
    """
    doomed: list[str] = []
    unexplained: list[str] = []
    unowned = {f"{PUBLIC_PREFIX}{path}" for path in UNOWNED_PATHS}
    for key in sorted(existing):
        if key in staged_keys or key in unowned:
            continue
        relative = key[len(PUBLIC_PREFIX) :]
        match = RECORD_RE.match(relative)
        if match is None:
            # A retired feed, or something left over from a publication that
            # stopped part way. Nothing promises either, so nothing has to
            # justify removing them.
            doomed.append(key)
        elif match.group(1) in tombstoned:
            doomed.append(key)
        else:
            unexplained.append(relative)
    if unexplained:
        raise RuntimeError(
            "refusing to withdraw published records with no tombstone:\n"
            + "\n".join(sorted(set(unexplained)))
        )
    return doomed


def _delete_old_snapshots(client: Any, bucket: str, keep: set[str]) -> None:
    """Retire snapshot prefixes other than the ones named.

    The prefix is asserted because this splits a key and deletes on the second
    component. Pointed at `public/` that component is `entries` or `renders`,
    and one call would take the whole corpus with it.
    """
    prefix = SNAPSHOT_PREFIX
    if prefix != "snapshots/":
        raise RuntimeError("release retention may only ever walk the snapshot prefix")
    doomed = [
        key
        for key in _list_keys(client, bucket, prefix)
        if len(key.split("/", 2)) >= 2 and key.split("/", 2)[1] not in keep
    ]
    if doomed:
        _delete_keys(client, bucket, doomed)


def _check_arithmetic(delta: dict[str, Any], parent: dict[str, Any]) -> None:
    """That this release is the parent plus what it says, and nothing else.

    The membership invariant, proved from two small documents rather than from
    a listing of everything. A stager that silently dropped records would have
    to declare withdrawals to keep the count, and would have to declare the
    right ones to keep the root; one that substituted a record for another
    would keep the count and lose the root. Neither can be arranged by
    accident, and neither costs anything proportional to the registry.
    """
    # One record is one entry file, however many render and evidence objects
    # come with it, so the count is over those and the root is over all of them.
    entries = len([row for row in delta["additions"] if row["path"].startswith("entries/")])
    withdrawn = len([row for row in delta["withdrawals"] if row["path"].startswith("entries/")])
    expected = int(parent["records"]["count"]) + entries - withdrawn
    if expected != int(delta["records"]["count"]):
        raise RuntimeError(
            f"the release delta says {delta['records']['count']} active records, but its "
            f"parent had {parent['records']['count']} and this release adds {entries} and "
            f"withdraws {withdrawn}, which is {expected}"
        )
    expected_root = release_delta.root_after(
        parent["records"]["root"], delta["additions"], delta["withdrawals"]
    )
    if expected_root != delta["records"]["root"]:
        raise RuntimeError(
            "the release delta's root is not its parent's root plus what it declares. "
            "The set of published records is not what this release says it is."
        )


def _declared_removals(delta: dict[str, Any]) -> list[str]:
    """Bounded derived-key retirements from an accepted incremental release.

    Takedowns and restorations deliberately take the full, listing-backed path.
    The narrow path may retire only an exact derived stable key such as an
    emptied search head; it may neither withdraw a record nor write a
    tombstone.
    """
    if delta["withdrawals"]:
        raise RuntimeError(
            "incremental accepted releases may not withdraw records; stage a full release"
        )
    if any(TOMBSTONE_RE.fullmatch(row["path"]) for row in delta["stable"]):
        raise RuntimeError(
            "incremental accepted releases may not write tombstones; stage a full release"
        )
    if any(TOMBSTONE_RE.fullmatch(path) for path in delta["retired"]):
        raise RuntimeError(
            "incremental accepted releases may not retire tombstones; stage a full release"
        )
    doomed: list[str] = []
    unexplained: list[str] = []
    # A derived object needs no tombstone, and could not have one: nothing
    # promised it and it belongs to no record. What it does need is to be
    # something this publisher stages and owns, so that a stager bug cannot
    # reach a record, an availability manifest it shares the bucket with, or a
    # key outside the layout, by calling it a retirement.
    # Deleted after everything is written, so a path that was both staged and
    # retired would have this release's own object removed at the end of it.
    staged = {row["path"] for row in delta["stable"] + delta["additions"]}
    for path in delta["retired"]:
        if (
            path in staged
            or path in ALWAYS_WRITTEN
            or RECORD_RE.match(path)
            or classify(path) != "stable"
            or not SERVED_RE.fullmatch(path)
        ):
            unexplained.append(path)
        else:
            doomed.append(f"{PUBLIC_PREFIX}{path}")
    if unexplained:
        raise RuntimeError(
            "refusing to withdraw published records with no tombstone:\n"
            + "\n".join(sorted(set(unexplained)))
        )
    return doomed


def publish_snapshot(
    client: Any, bucket: str, site: pathlib.Path, database: pathlib.Path | None = None
) -> str:
    launched = _launched(database) if database is not None else True
    _assert_conditional_writes(client, bucket)
    release, delta, staged = _staged_objects(site.resolve())
    pointer = _read_pointer(client, bucket)
    previous = pointer[0] if pointer else None

    incremental = delta["parent"] is not None
    # A full release is the recovery authority: it states and reconciles the
    # complete current set, so a corrupt prior base must not prevent it from
    # replacing that base. An incremental release derives its arithmetic from
    # the prior base and therefore must authenticate it before writing.
    parent = _publication_base(client, bucket, pointer) if incremental else None
    if incremental:
        if parent is None:
            raise RuntimeError(
                f"this release claims {delta['parent'][:12]} as its parent, but the origin's "
                "authenticated base for it could not be read. Stage a full rebuild instead of guessing "
                "what the previous release contained."
            )
        if delta["parent"] != parent["release"]:
            raise RuntimeError(
                "this release was built against a different parent than the one the pointer "
                f"names: {delta['parent'][:12]} against {parent['release'][:12]}"
            )
        if delta["takedowns_git_blob"] != parent["takedowns_git_blob"]:
            raise RuntimeError(
                "this incremental release does not preserve its authenticated "
                "parent's takedown authority"
            )
        _check_arithmetic(delta, parent)

    # A full rebuild states the whole dataset, so it can be reconciled against
    # what is there. An incremental release states only what changed, and
    # listing the bucket to find that out would put the O(S) back that the
    # delta exists to remove.
    existing = _list_keys(client, bucket, PUBLIC_PREFIX) if not incremental else None

    immutable = [row for row in staged if row[2] == "immutable"]
    stable = [row for row in staged if row[2] == "stable"]
    snapshot = [row for row in staged if row[2] == "snapshot"]

    # Offered whether or not it is already there. R2 refuses the taken ones and
    # the readback settles whether what is there is what this release says, so
    # knowing in advance buys nothing and costs a listing.
    write = [(relative, digest) for relative, digest, _kind in immutable]

    # Every stable object the release staged, and no comparison against the
    # parent. This used to skip the ones whose digest matched the parent's row
    # for them, because a release staged every page in the dataset and only a
    # handful had moved. A release now stages only the pages it changed -- so
    # every row here is one that moved, and the parent's delta no longer names
    # the ones that did not.
    rewrite: list[tuple[str, str]] = [
        (relative, digest) for relative, digest, _kind in stable
    ]
    # A reader that has a head must be able to trust every page it names, so
    # the pages go first and the documents above them last. Otherwise a reader
    # who arrives mid-publication can be told a day holds nothing while the
    # record that filled it is already written.
    rewrite.sort(key=lambda row: (_rank(row[0]), row[0]))

    if incremental:
        # Exactly what the release declares it takes away, with the tombstones
        # to justify it. Nothing is inferred from a listing, so a stager bug
        # cannot express itself as a withdrawal here.
        doomed = _declared_removals(delta)
    else:
        staged_keys = {
            f"{PUBLIC_PREFIX}{relative}" for relative, _d, _k in immutable + stable
        }
        tombstoned = {
            match.group(1)
            for relative, _digest, _kind in stable
            if (match := TOMBSTONE_RE.match(relative))
        }
        if not launched:
            # Nothing has been promised yet, so anything the stager no longer
            # names is simply no longer part of the database.
            print("pre-launch: withdrawing records that are no longer staged, without tombstones")
            tombstoned = {
                match.group(1)
                for key in existing
                if (match := RECORD_RE.match(key[len(PUBLIC_PREFIX):]))
            }
        doomed = _removals(existing, staged_keys, tombstoned)

    # Written and read back one at a time, so that a publication holds one
    # object rather than the whole release. The readback is not deferred to a
    # second pass any more: keeping the bytes to compare against later is
    # exactly the thing that made memory grow with the dataset.
    created = 0
    for relative, digest in write:
        data = _staged_bytes(site, relative, digest)
        key = f"{PUBLIC_PREFIX}{relative}"
        if _put_object(client, bucket, key, data, digest, relative, create_only=True):
            created += 1
        # Created or refused. The refused ones are the reason: R2 kept whatever
        # is there, and this is where a publication finds out whether what is
        # there is what this release says should be.
        _verify(client, bucket, key, data, digest)

    for relative, digest in rewrite:
        data = _staged_bytes(site, relative, digest)
        key = f"{PUBLIC_PREFIX}{relative}"
        _put_object(client, bucket, key, data, digest, relative)
        _verify(client, bucket, key, data, digest)

    for relative, digest, _kind in snapshot:
        data = _staged_bytes(site, relative, digest)
        key = f"{SNAPSHOT_PREFIX}{release}/{relative}"
        _put_object(client, bucket, key, data, digest, relative)
        _verify(client, bucket, key, data, digest)
    publication_base = release_delta.base_of(delta)
    base_bytes = release_delta.canonical_base_bytes(publication_base)
    base_digest = release_delta.base_id(publication_base)
    base_key = f"{SNAPSHOT_PREFIX}{release}/{release_delta.BASE_PATH}"
    _put_object(
        client, bucket, base_key, base_bytes, base_digest, release_delta.BASE_PATH
    )
    _verify(client, bucket, base_key, base_bytes, base_digest)
    # The release delta remains the readiness marker and therefore the last
    # release object. The pointer is moved only after both it and the base
    # projection have been read back.
    delta_bytes = release_delta.canonical_bytes(delta)
    delta_key = f"{SNAPSHOT_PREFIX}{release}/{MANIFEST_PATH}"
    _put_object(client, bucket, delta_key, delta_bytes, _sha256(delta_bytes), MANIFEST_PATH)
    _verify(client, bucket, delta_key, delta_bytes, _sha256(delta_bytes))

    # Before the flip, so that a crash here leaves a record already gone rather
    # than an index that says it is gone while it is still being served.
    if doomed:
        _delete_keys(client, bucket, doomed)

    body = json.dumps(
        {
            "schema_version": POINTER_SCHEMA,
            "release": release,
            "publication_base": base_digest,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode() + b"\n"
    _put_object(client, bucket, POINTER_KEY, body, _sha256(body), POINTER_KEY)
    current = _read_pointer(client, bucket)
    if current is None or current != (release, POINTER_SCHEMA, base_digest):
        raise RuntimeError("R2 pointer verification failed")

    if previous == release:
        # A retry of a publication that already flipped. The pointer cannot say
        # what came before it any more, so keeping an extra prefix is the only
        # answer that does not destroy the release we would roll back to.
        print("this release is already current; leaving older snapshots in place")
    else:
        _delete_old_snapshots(client, bucket, {release} | ({previous} if previous else set()))
    print(
        f"{'incremental' if incremental else 'full'} release: created {created} record "
        f"object(s) of {len(write)} offered, wrote {len(rewrite)} page(s), "
        f"{len(snapshot)} aggregate(s), withdrew {len(doomed)}"
    )
    return release


def audit(client: Any, bucket: str) -> list[str]:
    """What the origin holds against what the current release says it should.

    This is the O(S) sweep the delta makes room for. Publication no longer
    lists the bucket, so nothing in the ordinary path would notice an object
    that went missing or one that should not be there; that job moves here,
    where it runs on a schedule instead of once per accepted result.

    The check is against the release root, not against a copy of the manifest.
    The root is an additive multiset hash over every immutable object, so
    recomputing it from what is actually in the bucket and comparing to what
    the current release claims establishes the whole set at once -- including
    a substitution that keeps the count, which comparing counts would not.
    """
    pointer = _read_pointer(client, bucket)
    if pointer is None:
        return ["there is no pointer, so the origin is serving nothing"]
    if pointer[1] != POINTER_SCHEMA:
        return [f"the pointer is schema {pointer[1]}, which this tool cannot audit"]
    delta = read_delta(client, bucket, pointer[0])
    if delta is None:
        return ["the current release delta is missing or unreadable"]

    problems: list[str] = []
    unowned = {f"{PUBLIC_PREFIX}{path}" for path in UNOWNED_PATHS}
    existing = _list_keys(client, bucket, PUBLIC_PREFIX)

    rows: list[dict[str, Any]] = []
    for key, size in sorted(existing.items()):
        if key in unowned:
            continue
        relative = key[len(PUBLIC_PREFIX):]
        kind = classify(relative)
        if kind == "stable":
            # Present and named by nothing. The delta describes what its
            # release wrote, so a page a release did not touch appears in no
            # delta at all -- which is the point of the change that trimmed it,
            # and the reason this check is now "is it a path we serve" rather
            # than "is it in the current delta". Whether its *bytes* are right
            # is `--reconcile`, which needs a rebuild to compare against and so
            # runs from the weekly sweep rather than from a health check.
            if not SERVED_RE.fullmatch(relative):
                problems.append(f"unexpected: {key}")
            continue
        if kind != "immutable":
            problems.append(f"unexpected: {key}")
            continue
        head = _head(client, bucket, key)
        digest = ((head or {}).get("Metadata") or {}).get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            problems.append(f"no recorded digest: {key}")
            continue
        rows.append({"path": relative, "bytes": size, "sha256": digest})

    observed = release_delta.root_of(rows)
    if observed != delta["records"]["root"]:
        problems.append(
            f"the origin holds {len(rows)} record object(s) whose root is {observed[:12]}, "
            f"but release {pointer[0][:12]} claims {delta['records']['root'][:12]}"
        )
    return problems


def reconcile(client: Any, bucket: str, site: pathlib.Path) -> list[str]:
    """What the origin is serving, against a full rebuild of the same dataset.

    The check a release used to make on itself, moved to where it belongs. A
    release described itself with a row per stable object in the whole dataset,
    so it could tell a page that had drifted from one that had not -- and paid
    O(S) for that once per accepted result, which is the O(S²) the delta exists
    to remove. This is the same question asked weekly instead, against a tree
    staged from the records rather than against a copy of the last answer, so
    it also catches a page that has been wrong since it was written.

    It reads and changes nothing. A publication that has to *fix* what this
    finds is a `--full` one, which lists the bucket and reconciles against it
    on its own account.
    """
    problems: list[str] = []
    expected: dict[str, tuple[int, str]] = {}
    for path in sorted(site.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(site).as_posix()
        if relative in UNOWNED_PATHS or classify(relative) != "stable":
            continue
        data = path.read_bytes()
        expected[f"{PUBLIC_PREFIX}{relative}"] = (len(data), _sha256(data))

    existing = _list_keys(client, bucket, PUBLIC_PREFIX)
    served = {
        key
        for key in existing
        if classify(key[len(PUBLIC_PREFIX):]) == "stable"
        and key[len(PUBLIC_PREFIX):] not in UNOWNED_PATHS
    }
    for key in sorted(set(expected) - served):
        problems.append(f"missing: {key}")
    for key in sorted(served - set(expected)):
        problems.append(f"unexpected: {key}")
    for key in sorted(set(expected) & served):
        size, digest = expected[key]
        if existing[key] != size:
            problems.append(f"wrong size: {key}")
            continue
        raw = _read_object(client, bucket, key)
        if raw is None or _sha256(raw) != digest:
            problems.append(f"wrong bytes: {key}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=pathlib.Path)
    parser.add_argument(
        "--database",
        type=pathlib.Path,
        default=pathlib.Path("."),
        help="the canonical database, read only to see whether it has launched",
    )
    parser.add_argument("--audit", action="store_true", help="report drift and change nothing")
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="compare every served page against the full rebuild given by --site, "
        "and change nothing. This is the whole-set check a release stopped making "
        "when its delta stopped naming every object in the dataset.",
    )
    parser.add_argument(
        "--fetch-prior",
        type=pathlib.Path,
        help="a file naming the published documents the next staging run patches, "
        "one per line. They are written under --into and nothing else is read.",
    )
    parser.add_argument("--into", type=pathlib.Path)
    parser.add_argument(
        "--write-current-base",
        type=pathlib.Path,
        help="write the authenticated constant-size base of the release being served "
        "here and change nothing, so "
        "that the next staging run can describe itself as a difference from it",
    )
    parser.add_argument("--bucket", default=os.environ.get("R2_BUCKET", BUCKET_DEFAULT))
    args = parser.parse_args()
    reading_only = (
        args.audit or args.write_current_base is not None or args.fetch_prior is not None
    )
    if args.reconcile and args.site is None:
        parser.error("--reconcile needs the full rebuild to compare against, in --site")
    if args.fetch_prior is not None and args.into is None:
        parser.error("--fetch-prior needs somewhere to put them, in --into")
    if not reading_only and not args.reconcile and args.site is None:
        parser.error("--site is required unless --audit or --write-current-base is given")
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
    if args.write_current_base is not None:
        # The staging run needs to know what is already published so it can
        # describe itself as a difference. It has no bucket credentials, which
        # is deliberate, so the authenticated constant-size base projection is
        # handed to it as a file rather than the potentially full release delta.
        pointer = _read_pointer(client, args.bucket)
        try:
            base = _publication_base(client, args.bucket, pointer)
        except RuntimeError as error:
            # Content that disagrees with its pointer cannot authorize a
            # scoped publication. Treat it as no usable base here so a manual
            # or automatic run can take the complete listing-backed path.
            # The publishing call itself keeps the hard failure for a staged
            # incremental delta; only this read-only planning handoff falls
            # back.
            print(f"::warning::served publication base is unusable: {error}")
            base = None
        if base is None:
            args.write_current_base.write_bytes(b"")
            print("no usable publication base; the next release will be a full one")
        else:
            args.write_current_base.write_bytes(release_delta.canonical_base_bytes(base))
            print(f"the current release is {pointer[0]}, holding {base['records']['count']} records")
        return 0
    if args.fetch_prior is not None:
        # The only credentialed step of a staging run, and it decides nothing.
        # Which documents to fetch was worked out by a step with no access to
        # this bucket, and what to do with them is worked out by another one.
        # A document that is not there is simply not written: the stager treats
        # a missing one as never published, or falls back to a rebuild, and
        # deciding which of those is right is not this step's business.
        wanted = [
            line.strip()
            for line in args.fetch_prior.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        found = 0
        for relative in wanted:
            if relative.startswith("/") or ".." in relative.split("/"):
                raise SystemExit(f"refusing to fetch an unsafe path: {relative}")
            raw = _read_object(client, args.bucket, f"{PUBLIC_PREFIX}{relative}")
            if raw is None:
                continue
            target = args.into / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            found += 1
        print(f"fetched {found} of {len(wanted)} prior document(s)")
        return 0
    if args.audit or args.reconcile:
        problems = (
            audit(client, args.bucket)
            if args.audit
            else reconcile(client, args.bucket, args.site.resolve())
        )
        for line in problems:
            print(line)
        print(f"audit found {len(problems)} problem(s)")
        return 1 if problems else 0
    release = publish_snapshot(client, args.bucket, args.site, args.database)
    print(f"published release {release}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
