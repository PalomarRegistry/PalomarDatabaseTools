"""Reconcile render and evidence trees with entry-declared bundle roots.

Per-entry validators own the bytes and policy inside one content-addressed
bundle.  This module owns the cross-entry filesystem contract: every node in
the render and evidence trees must be a declared root, lie inside one, or be
an ancestor on the way to one.  Complete validation also enforces the
database-wide node caps; scoped validation examines only the immutable paths
that arrived in the accepted delta.

The boundary is read-only and returns ordered diagnostics.  Reference tests
use component-prefix set lookups, so the work per node does not grow with the
number of registered bundles.
"""

from __future__ import annotations

import pathlib

import evidence_validation
import render_validation


def _reference_test(roots: set[str]):
    """Return whether a filesystem node is accounted for by these roots.

    A node is accounted for if it *is* a root, if it is inside one, or if it
    is on the way to one -- ``renders/<id>-vN`` is a real directory with no
    root of its own.  Prefixes are taken at component boundaries so a version
    ending in ``v11`` is not read as being inside ``v1``.  Root depths are
    discovered rather than assumed so malformed shapes still get the same
    reference answer before their owning validator reports the shape error.
    """
    ancestors: set[str] = set()
    for root in roots:
        parts = root.split("/")
        for depth in range(1, len(parts)):
            ancestors.add("/".join(parts[:depth]))
    depths = sorted({len(root.split("/")) for root in roots})

    def referenced(relative: str) -> bool:
        if relative in ancestors:
            return True
        parts = relative.split("/")
        return any(
            "/".join(parts[:depth]) in roots
            for depth in depths
            if depth <= len(parts)
        )

    return referenced


def validate_bundle_references(
    root: pathlib.Path,
    render_roots: set[str],
    evidence_roots: set[str],
    *,
    frozen_paths: frozenset[str] | None = None,
) -> list[str]:
    """Return cross-entry render/evidence tree errors.

    ``frozen_paths=None`` requests the complete proof, including each tree's
    database-wide node cap.  A frozen-path set is the exact accepted delta.
    ``validation_scope.scope_of`` permits that path only when every frozen
    change is an addition belonging to the new entries, so neither a
    historical node nor the entry that references it can have disappeared;
    only arriving nodes can therefore be newly orphaned.
    """
    errors: list[str] = []
    for directory, roots, noun, per_root in (
        (
            "renders",
            render_roots,
            "render artifact",
            # The record-version and digest directories sit below renders/.
            render_validation.MAX_RENDER_NODES + 2,
        ),
        (
            "evidence",
            evidence_roots,
            "verification evidence",
            evidence_validation.MAX_EVIDENCE_NODES,
        ),
    ):
        tree = root / directory
        if tree.is_symlink():
            errors.append(
                f"{directory}/: must be an ordinary directory, not a symbolic link"
            )
            continue
        if tree.exists() and not tree.is_dir():
            errors.append(f"{directory}/: must be a directory")
            continue
        if not tree.is_dir():
            continue

        if frozen_paths is None:
            nodes: list[pathlib.Path] = []
            limit = max(1, len(roots)) * per_root
            for path in tree.rglob("*"):
                nodes.append(path)
                if len(nodes) > limit:
                    errors.append(
                        f"{directory}/: exceeds the database-wide filesystem-node cap"
                    )
                    break
        else:
            # Exactly what this change brought, including an orphan not named
            # by any entry. The cap is a whole-database property and is left
            # to the complete run, which alone can answer it.
            nodes = [
                root / relative
                for relative in sorted(frozen_paths)
                if relative.startswith(f"{directory}/")
            ]

        referenced = _reference_test(roots)
        for path in sorted(set(nodes)):
            relative = path.relative_to(root).as_posix()
            if not referenced(relative):
                errors.append(f"{relative}: {noun} is not referenced by any entry")
    return errors
