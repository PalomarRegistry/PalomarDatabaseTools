#!/usr/bin/env python3
"""Which record objects are new since the commit a release was built from.

The append-only invariant says `entries/`, `renders/` and `evidence/` only ever
grow. That is exactly the property that makes a git diff a complete answer to
"what does this release add", in time proportional to what changed rather than
to the size of the registry -- which is the whole reason staging can stop
copying and re-hashing the entire dataset on every publication.

The invariant is not assumed here, it is checked: any change under those paths
that is not an addition means either a takedown, which arrives through
`takedowns.json` and not through this, or something has gone wrong. Both answer
"I cannot tell", and the caller rebuilds from scratch. Falling back is always
correct and merely slow, so it is the answer to every uncertainty: an unknown
parent commit, a dirty tree, a repository that is not one, a shallow clone that
does not reach back far enough.
"""

from __future__ import annotations

import pathlib
import subprocess

RECORD_DIRECTORIES = ("entries", "renders", "evidence")


class CannotTell(Exception):
    """Raised when the diff would not be a complete answer."""


def _git(root: pathlib.Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except UnicodeDecodeError as error:
        raise CannotTell(f"git {' '.join(args)}: output is not UTF-8") from error
    if result.returncode:
        raise CannotTell(f"git {' '.join(args)}: {result.stderr.strip() or 'failed'}")
    return result.stdout


def require_clean(root: pathlib.Path, paths: tuple[str, ...]) -> None:
    """Require worktree bytes and modes under ``paths`` to equal HEAD."""
    if not (root / ".git").exists():
        raise CannotTell(f"{root} is not a git checkout")
    status = _git(root, "status", "--porcelain", "--", *paths)
    if status.strip():
        raise CannotTell(
            "the working tree has uncommitted changes under " + ", ".join(paths)
        )


def changed_since(root: pathlib.Path, base: str) -> set[str]:
    """Every path that differs between `base` and the working tree.

    Unlike `ordinary_additions_since` this makes no claim about the append-only invariant
    and accepts any kind of change, because its caller is deciding what to
    check rather than what to publish. It still raises rather than guessing:
    a partial answer here is a validator that quietly stops looking at things.
    """
    if not (root / ".git").exists():
        raise CannotTell(f"{root} is not a git checkout")
    try:
        _git(root, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    except CannotTell as error:
        raise CannotTell(f"the base revision is not in this clone: {base}") from error
    changed = set()
    for listing in (
        # A rename is a deletion plus an addition for validation purposes. Do
        # not let Git's similarity heuristic hide the source path, especially
        # when a policy file is moved into a record directory.
        _git(root, "diff", "--name-only", "-z", "--no-renames", base, "--"),
        # Anything not committed yet is part of what a check has to look at,
        # and `git diff <base>` alone would not mention it.
        _git(root, "status", "--porcelain", "-z", "--untracked-files=all"),
    ):
        for record in listing.split("\0"):
            if not record:
                continue
            # `git diff --name-only` gives a bare path; `git status --porcelain`
            # gives two status characters, a space, then the path. Stripping
            # first would lose the offset, because an unstaged change begins
            # with a space of its own.
            changed.add(record[3:] if record[2:3] == " " else record)
    return changed


def committed_changes_since(root: pathlib.Path, base: str) -> set[str]:
    """Every committed path change from an ancestor to ``HEAD``.

    Publication stages committed database state. Its output may deliberately
    sit below a test checkout, so unrelated untracked files are not part of
    this answer; the append-only directory checks still reject dirty canonical
    record trees separately.
    """
    if not (root / ".git").exists():
        raise CannotTell(f"{root} is not a git checkout")
    try:
        _git(root, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    except CannotTell as error:
        raise CannotTell(f"the base revision is not in this clone: {base}") from error
    ancestry = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", base, "HEAD"],
        check=False,
        capture_output=True,
    )
    if ancestry.returncode:
        raise CannotTell(f"{base} is not an ancestor of HEAD")
    return {
        path
        for path in _git(
            root, "diff", "--name-only", "-z", "--no-renames", base, "HEAD", "--"
        ).split("\0")
        if path
    }


def changed_bundle_roots(
    paths: set[str] | frozenset[str], directory: str
) -> frozenset[str]:
    """Immutable bundle roots touched by nested file paths, in O(changed)."""
    roots = set()
    prefix = f"{directory}/"
    for relative in paths:
        if not relative.startswith(prefix):
            continue
        parts = relative.split("/")
        if len(parts) >= 3:
            roots.add("/".join(parts[:3]))
    return frozenset(roots)


def ordinary_additions_since(
    root: pathlib.Path,
    parent_commit: str,
    directories: tuple[str, ...] = RECORD_DIRECTORIES,
) -> set[str]:
    """Ordinary files added under ``directories`` since ``parent_commit``.

    Raises `CannotTell` rather than returning a partial answer. A partial
    answer here is a release that silently omits records, which is the one
    outcome worse than a slow publication.
    """
    if not (root / ".git").exists():
        raise CannotTell(f"{root} is not a git checkout")

    # The committed tree is what publication and push validation judge. A
    # modified or untracked frozen path would otherwise appear in one answer
    # without being attested by the other.
    require_clean(root, directories)

    try:
        _git(root, "rev-parse", "--verify", "--quiet", f"{parent_commit}^{{commit}}")
    except CannotTell as error:
        raise CannotTell(f"the base commit is not in this clone: {parent_commit}") from error
    ancestry = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", parent_commit, "HEAD"],
        check=False,
        capture_output=True,
    )
    if ancestry.returncode:
        raise CannotTell(f"{parent_commit} is not an ancestor of HEAD")

    # `--no-renames`, because a rename of a published record is a delete and an
    # add, and reporting it as a rename would hide the delete.
    raw = _git(
        root, "diff", "--raw", "-r", "-z", "--no-renames",
        parent_commit, "HEAD", "--", *directories,
    )
    fields = raw.split("\0")
    added: set[str] = set()
    index = 0
    while index < len(fields) and fields[index]:
        meta = fields[index]
        path = fields[index + 1] if index + 1 < len(fields) else ""
        index += 2
        # :<old mode> <new mode> <old sha> <new sha> <status>
        parts = meta.lstrip(":").split()
        if len(parts) != 5:
            raise CannotTell(f"unreadable diff record: {meta!r}")
        old_mode, new_mode, _old, _new, status_letter = parts
        if status_letter != "A":
            raise CannotTell(
                f"{path}: {status_letter} is not an addition, so this is not an append-only "
                "step and the diff does not describe the release"
            )
        if old_mode != "000000" or new_mode != "100644":
            raise CannotTell(f"{path}: added as mode {new_mode}, which is not an ordinary file")
        added.add(path)
    return added
