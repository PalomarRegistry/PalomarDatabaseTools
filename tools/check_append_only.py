#!/usr/bin/env python3
"""Enforce Palomar's append-only publication invariant over a git range.

Published entry and evidence files never change: not their bytes, not their paths. The sole
entry schema freezes at launch. Everything else in
the repository may change freely. `docs/append-only.md` states the invariant and
the reasoning behind it.

Three modes:

    check_append_only.py --base <rev> --head <rev>
        Verify that going from <rev> to <rev> preserves the invariant.

    check_append_only.py --history <rev>
        Verify every step along the first-parent chain ending at <rev>.

    check_append_only.py --tree <rev>
        Verify that every frozen path in <rev> is an ordinary file, which is
        the one violation a diff between two revisions cannot see.

The first mode reads a diff and costs what the commit changed. The other two
read whole trees and cost what the registry holds, so they run on a schedule.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import subprocess
import sys

ENTRIES_PREFIX = "entries/"
RENDERS_PREFIX = "renders/"
EVIDENCE_PREFIX = "evidence/"
# Frozen but never served. The scores that decided a version are kept so the
# review outcome stays reconstructable; they are not part of the record, so that a
# record's bytes are a function of the commit rather than of the publisher.
SCORES_PREFIX = "scores/"
# Submission ids are retry/idempotency keys. Once one is bound to a registered
# version it must never be rebound, while result and day projections continue
# through validator-owned exact transitions.
SUBMISSIONS_PREFIX = "registrations/submissions/"
# A migration manifest is the permanent evidence for a change that crossed one
# of these promises, and it is what says the crossing has already happened. Both
# uses need it to be immutable: an audit reading it years later, and the schema
# exception below, whose "the manifest is not in the base" test means "this has
# never been done" only because a manifest that exists can never stop existing.
MIGRATIONS_PREFIX = "migrations/"

# A prefix, and what to say when a file under it is removed, changed, or has
# its mode changed. The sentences are written out rather than composed, because
# each one has to tell a contributor what to do instead.
FROZEN = (
    (
        ENTRIES_PREFIX,
        "published entries are never deleted or renamed. "
        "Corrections publish a new version instead.",
        "published entries are immutable, byte for byte. "
        "Corrections publish a new version instead.",
        "the file mode of a published entry may not change",
    ),
    (
        RENDERS_PREFIX,
        "published render artifacts are never deleted or renamed",
        "published render artifacts are immutable, byte for byte",
        "the file mode of a published render artifact may not change",
    ),
    (
        EVIDENCE_PREFIX,
        "published verification evidence is never deleted or renamed",
        "published verification evidence is immutable, byte for byte",
        "the file mode of published verification evidence may not change",
    ),
    (
        SCORES_PREFIX,
        "recorded review scores are never deleted or renamed. "
        "They are what makes the review outcome reconstructable.",
        "recorded review scores are immutable, byte for byte. "
        "A re-review registers a new version, which records its own scores.",
        "the file mode of recorded review scores may not change",
    ),
    (
        SUBMISSIONS_PREFIX,
        "registered submission bindings are never deleted or renamed",
        "registered submission bindings are immutable, byte for byte",
        "the file mode of a registered submission binding may not change",
    ),
    (
        MIGRATIONS_PREFIX,
        "an adopted migration manifest is never deleted or renamed. "
        "It is the permanent evidence for a change that crossed a promise.",
        "an adopted migration manifest is immutable, byte for byte. "
        "It records exactly which bytes the crossing it authorized moved.",
        "the file mode of an adopted migration manifest may not change",
    ),
)
ENTRY_SCHEMA_NAME = "schema-v3.json"
ENTRY_SCHEMA_NAMES = frozenset({ENTRY_SCHEMA_NAME, "schema-v4.json"})
LAUNCH_MARKER = ".palomar-launched"
# The schema transitions this check will accept, and only when the change that
# makes one also introduces its named manifest binding it. The manifest names the
# exact bytes on both sides, so recognising it here is a digest comparison
# rather than a judgement: a later change finds the manifest already in its base
# and the schema is frozen again, over the new bytes.
#
# What this does not check is that the transition is a widening. That takes a
# schema library and the whole published ledger, which is
# widening verifier in the database, run in place of the base revision's checker
# for the change that lands it. This check is what keeps the same commit legal
# for every later walk of the history.
SCHEMA_WIDENING_MANIFESTS = (
    "migrations/classification-cardinality-v1.json",
    "migrations/orcid-record-check-v1.json",
)

# A published file must be an ordinary, non-executable file. A symlink freezes
# only its target string, leaving the bytes a consumer actually reads mutable;
# a gitlink freezes nothing at all.
REGULAR_FILE = "100644"

def _git(repo: pathlib.Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _tree(repo: pathlib.Path, rev: str) -> dict[str, tuple[str, str]]:
    """Map every path in `rev` to its (mode, object id)."""
    contents: dict[str, tuple[str, str]] = {}
    for record in _git(repo, "ls-tree", "-r", "-z", rev).split("\0"):
        if not record:
            continue
        meta, path = record.split("\t", 1)
        mode, _kind, oid = meta.split(" ")
        contents[path] = (mode, oid)
    return contents


def _describe(mode: str) -> str:
    return {
        "120000": "a symbolic link",
        "160000": "a submodule",
        "100755": "an executable file",
    }.get(mode, f"mode {mode}")


def _is_frozen_path(path: str) -> bool:
    return (
        path.startswith(tuple(prefix for prefix, *_messages in FROZEN))
        or path in ENTRY_SCHEMA_NAMES
    )


def _listed(repo: pathlib.Path, rev: str, path: str) -> tuple[str, str] | None:
    """The (mode, object id) of `path` in `rev`, or None if the path is absent.

    `ls-tree` rather than `show`, because absence and an unreadable blob are
    different answers and only the first one may relax anything. A missing blob
    behind a listed path raises from `_git_object` instead of quietly reading as
    absence, which would re-arm the exception in a damaged clone.
    """
    listing = _git(repo, "ls-tree", "-z", rev, "--", path).strip("\0")
    if not listing:
        return None
    meta, _tab, _name = listing.partition("\t")
    mode, _kind, oid = meta.split(" ")
    return mode, oid


def _widening_manifested(
    repo: pathlib.Path, base: str, head: str, old_oid: str, new_oid: str
) -> bool:
    """Whether `head` introduces a manifest binding exactly this schema change.

    Four things have to hold, and each of them is what stops this from becoming
    a general licence to edit the schema. The manifest must be absent from the
    base and added by this change, so it authorizes one transition rather than
    standing open. It must be an ordinary file, for the reason every frozen path
    must be. It must name the entry schema. And it must carry the digest of the
    bytes on each side, which makes recognising it a comparison rather than a
    judgement: bytes that do not hash to what the manifest says are not the
    transition it authorizes.

    What makes "absent from the base" mean "never introduced" rather than "not
    introduced yet" is that the manifest is itself a frozen path: once added it
    can never be removed, so the exception can be armed exactly once. Without
    that, deleting the manifest and reintroducing it beside a second schema edit
    would pass both checks.
    """
    digests = {
        side: hashlib.sha256(_git_object(repo, oid)).hexdigest()
        for side, oid in (("old_sha256", old_oid), ("new_sha256", new_oid))
    }
    matches = 0
    for manifest_path in SCHEMA_WIDENING_MANIFESTS:
        if _listed(repo, base, manifest_path) is not None:
            continue
        listed = _listed(repo, head, manifest_path)
        if listed is None or listed[0] != REGULAR_FILE:
            continue
        try:
            manifest = json.loads(_git_object(repo, listed[1]))
        except ValueError:
            continue
        change = manifest.get("schema_change") if isinstance(manifest, dict) else None
        if (
            isinstance(change, dict)
            and change.get("path") == ENTRY_SCHEMA_NAME
            and all(change.get(side) == digest for side, digest in digests.items())
        ):
            matches += 1
    return matches == 1


def _git_object(repo: pathlib.Path, oid: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), "cat-file", "blob", oid],
        check=True,
        capture_output=True,
    ).stdout


def _changes(repo: pathlib.Path, base: str, head: str) -> list[tuple[str, str, str, str, str, str]]:
    """Every path that differs between two revisions, and how.

    A raw diff, not two whole trees. Reading both trees costs O(everything) on
    a check that runs once per push, and `--history` did it once per commit,
    which is O(S²) over the registry's life. Only what changed can break an
    invariant about change.

    `--no-renames` because a renamed published record is a delete and an add,
    and reporting it as a rename would hide the delete. `-M`/`-C` are therefore
    deliberately absent. `-r` recurses into subdirectories, without which a
    changed render bundle appears as one directory entry.
    """
    raw = _git(repo, "diff-tree", "-r", "--raw", "--no-renames", "-z", base, head)
    fields = raw.split("\0")
    changes: list[tuple[str, str, str, str, str, str]] = []
    index = 0
    while index < len(fields) and fields[index]:
        meta = fields[index]
        path = fields[index + 1] if index + 1 < len(fields) else ""
        index += 2
        parts = meta.lstrip(":").split()
        if len(parts) != 5 or not path:
            raise RuntimeError(f"unreadable diff record: {meta!r}")
        old_mode, new_mode, old_oid, new_oid, status = parts
        changes.append((path, status[:1], old_mode, new_mode, old_oid, new_oid))
    return changes


def check(repo: pathlib.Path, base: str, head: str) -> list[str]:
    """Return every way in which `head` breaks the invariant relative to `base`.

    The invariant is a promise made at launch, and `.palomar-launched` is that
    promise. Before it exists there is nothing to break: a pre-launch database
    may be reshaped or emptied freely. `check_history` has always read the
    marker this way; this comparison did not, which made the invariant bind
    before anyone had relied on it.

    This reads a diff rather than two trees, so its cost is what the commit
    changed rather than what the registry holds. What a diff cannot see is a
    violation that was already there when `base` was written -- a symlink that
    has sat under `entries/` since before the marker, say. `check_tree` is what
    looks for those, and it runs on a schedule rather than per push.
    """
    if not _has_marker(repo, base):
        return []
    errors: list[str] = []
    def not_ordinary(path: str, mode: str) -> str:
        return (
            f"{path}: is {_describe(mode)}; frozen files must be ordinary "
            "files, so that freezing the path freezes the bytes."
        )

    for path, status, old_mode, new_mode, old_oid, new_oid in _changes(repo, base, head):
        if not _is_frozen_path(path):
            continue

        # A file that is not an ordinary file cannot be frozen at all: freezing
        # a symlink pins the target string while the bytes behind it stay
        # mutable. A diff sees a path only when it changes, so an addition is
        # the one moment this can be judged; `check_tree` covers the ones that
        # were already there when the promise was made.
        if status in ("A", "T") and new_mode != REGULAR_FILE:
            errors.append(not_ordinary(path, new_mode))
            continue

        if path in ENTRY_SCHEMA_NAMES:
            if status == "D":
                errors.append(
                    f"{path}: deleted, but it is the sole published entry schema and is frozen"
                )
            elif status == "A":
                # A new versioned contract may be introduced without changing
                # the bytes or meaning of any older contract. From this commit
                # onward the ordinary-file addition is frozen like its peers.
                if path == "schema-v4.json":
                    continue
                errors.append(f"{path}: added after launch, but the published entry schema is frozen")
            elif old_mode != new_mode:
                errors.append(f"{path}: the file mode of the published entry schema may not change")
            elif not _widening_manifested(repo, base, head, old_oid, new_oid):
                errors.append(
                    f"{path}: modified, but it is the sole published entry schema and is "
                    "frozen except by a manifested widening"
                )
            continue

        _prefix, on_delete, on_change, on_chmod = next(
            row for row in FROZEN if path.startswith(row[0])
        )
        if status == "D":
            errors.append(f"{path}: {on_delete}")
        elif status == "A":
            continue
        elif old_mode != new_mode:
            # Same bytes, different mode. Reported as the mode rule rather than
            # the immutability rule, because that is what the contributor did.
            errors.append(f"{path}: {on_chmod}")
        else:
            errors.append(f"{path}: {on_change}")

    return errors


def check_tree(repo: pathlib.Path, rev: str) -> list[str]:
    """Look for a violation a diff could never see.

    Every frozen path in one revision, checked for being an ordinary file. A
    per-push diff only sees what that push touched, so a symlink committed
    before the launch marker, or before this check existed, would never be
    looked at again. This is the sweep that looks at all of it, and it is why
    the per-push check is allowed to be cheap.
    """
    if not _has_marker(repo, rev):
        return []
    return [
        f"{path}: is {_describe(mode)}; frozen files must be ordinary "
        "files, so that freezing the path freezes the bytes."
        for path, (mode, _oid) in sorted(_tree(repo, rev).items())
        if mode != REGULAR_FILE and _is_frozen_path(path)
    ]


def _has_marker(repo: pathlib.Path, rev: str) -> bool:
    """Whether the launch promise had been made as of `rev`.

    One path out of the tree rather than the whole tree: `ls-tree` on a single
    path is O(1) in the size of the repository, and this runs on every check.
    """
    return bool(_git(repo, "ls-tree", "--name-only", rev, "--", LAUNCH_MARKER).strip())


def _announce_marker(repo: pathlib.Path, rev: str, *, scope: str) -> bool:
    """Say, on every run, whether the promise has been made yet.

    Returning no violations because the marker is absent looks exactly like
    finding none, and it looks that way in a green job that nobody reads twice.
    The disarmed state is deliberate, and it is also the state in which every
    published file in this repository may be rewritten or deleted with CI
    saying the invariant holds. That has to be visible from the run, not from
    knowing the code.

    A `::warning::` because GitHub turns it into an annotation on the run and
    on the step, so it is visible from the checks view without opening the log,
    and because it is an ordinary sentence in the log for the same command run
    by hand.

    `scope` because the modes do not all mean the same thing by the marker
    being absent. A comparison and a tree sweep looked at nothing at all; the
    history walk reads the marker again at every step, so an absent one at the
    tip says the invariant does not bind now, not that nothing was examined.
    """
    if _has_marker(repo, rev):
        return True
    print(
        f"::warning::append-only enforcement is OFF: {LAUNCH_MARKER} is absent from {rev}, "
        f"so {scope}. This is deliberate before launch; committing the marker is a launch "
        "step, and docs/append-only.md has the order to do it in."
    )
    return False


def check_history(repo: pathlib.Path, rev: str) -> list[str]:
    """Check every first-parent step in the history ending at `rev`.

    Each step is a diff, so the whole walk costs what the registry has ever
    changed rather than its size once per commit. That is the difference
    between O(S) for the walk and O(S²), and it is why this can stay a real
    check instead of becoming a sampling one.

    It is still O(S), so it runs on a schedule rather than on every push. The
    per-push check is the diff from the previous tip, which is what actually
    blocks a bad change; this is what keeps an older one red.
    """
    errors: list[str] = []
    for line in _git(repo, "log", "--first-parent", "--format=%H %P", rev).splitlines():
        commit, *parents = line.split()
        if not parents:
            continue
        for error in check(repo, parents[0], commit):
            errors.append(f"{commit[:12]}: {error}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        type=pathlib.Path,
        default=pathlib.Path("."),
        help="repository to inspect (default: the current directory)",
    )
    parser.add_argument("--base", help="revision the change starts from")
    parser.add_argument("--head", help="revision the change ends at")
    parser.add_argument("--history", metavar="REV", help="check every first-parent step ending at REV")
    parser.add_argument(
        "--tree",
        metavar="REV",
        help="check every frozen path in REV for being an ordinary file, which is "
        "the one violation a diff cannot see",
    )
    args = parser.parse_args(argv)

    # The revision whose tree decides whether anything is enforced at all. For a
    # comparison that is the base, which is what `check` reads: the promise binds
    # from where the change starts, not from where it ends, or a commit could
    # make the promise and break it at once.
    if args.tree:
        if args.base or args.head or args.history:
            parser.error("--tree cannot be combined with the other modes")
        armed = _announce_marker(
            args.repo, args.tree, scope="no frozen path was looked at"
        )
        errors = check_tree(args.repo, args.tree)
        checked = f"the frozen paths of {args.tree}"
        unchecked = f"append-only enforcement is off: nothing was checked for {checked}"
    elif args.history:
        if args.base or args.head:
            parser.error("--history cannot be combined with --base or --head")
        # The walk reads the marker again at every step, so this one is about
        # now rather than about the whole walk: a history that carried the
        # marker and then lost it was still checked where it carried it.
        armed = _announce_marker(
            args.repo,
            args.history,
            scope="a change made now is compared against nothing, and only "
            "transitions whose base carried the marker were checked",
        )
        errors = check_history(args.repo, args.history)
        checked = f"the first-parent history of {args.history}"
        unchecked = (
            "append-only enforcement is off at the tip: of "
            f"{checked}, only transitions whose base carried the marker were checked"
        )
    elif args.base and args.head:
        armed = _announce_marker(
            args.repo, args.base, scope="this change was compared against nothing"
        )
        errors = check(args.repo, args.base, args.head)
        checked = f"{args.base} to {args.head}"
        unchecked = f"append-only enforcement is off: nothing was checked for {checked}"
    else:
        parser.error("pass --history REV, --tree REV, or both --base REV and --head REV")

    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        print(f"\nappend-only invariant violated by {checked}", file=sys.stderr)
        return 1
    # Not "holds". A line saying the invariant holds is the sentence somebody
    # will remember having read, on the day it stopped being true.
    if not armed:
        print(unchecked)
        return 0
    print(f"append-only invariant holds for {checked}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
