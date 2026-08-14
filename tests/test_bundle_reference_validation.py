"""Render/evidence filesystem nodes are accounted for by entry roots.

This replaced a scan of every referenced root for every node, which is
quadratic in the size of the database and ran four times per accepted entry.
The replacement has to answer *identically*, so the old predicate is kept here
as an oracle and the two are compared over a corpus built to be awkward.
"""

from __future__ import annotations

import pytest
from bundle_reference_validation import _reference_test, validate_bundle_references

TREE = "a" * 64
OTHER = "b" * 64


def oracle(roots: set[str], relative: str) -> bool:
    """The predicate this replaced, verbatim."""
    return any(
        relative == expected
        or relative.startswith(f"{expected}/")
        or expected.startswith(f"{relative}/")
        for expected in roots
    )


ROOTS = {
    f"renders/PALOMAR-2026-08-07-000001-v1/{TREE}",
    f"renders/PALOMAR-2026-08-07-000001-v2/{TREE}",
    f"renders/PALOMAR-2026-08-07-000011-v1/{OTHER}",
}

NODES = [
    # the roots themselves
    f"renders/PALOMAR-2026-08-07-000001-v1/{TREE}",
    # inside a root, at several depths
    f"renders/PALOMAR-2026-08-07-000001-v1/{TREE}/Challenge",
    f"renders/PALOMAR-2026-08-07-000001-v1/{TREE}/Challenge/index.html",
    f"renders/PALOMAR-2026-08-07-000001-v1/{TREE}/-verso-search/search-box.css",
    # on the way to a root: a real directory with no root of its own
    "renders/PALOMAR-2026-08-07-000001-v1",
    "renders/PALOMAR-2026-08-07-000011-v1",
    # a version prefix that must NOT be read as being inside another
    "renders/PALOMAR-2026-08-07-000001-v11",
    f"renders/PALOMAR-2026-08-07-000001-v11/{TREE}",
    # an id prefix, likewise
    "renders/PALOMAR-2026-08-07-00000",
    # strays at every depth
    "renders/stray.txt",
    "renders/PALOMAR-2026-08-07-000001-v1/stray.txt",
    f"renders/PALOMAR-2026-08-07-000001-v1/{OTHER}",
    f"renders/PALOMAR-2026-08-07-000001-v1/{OTHER}/Challenge/index.html",
    f"renders/PALOMAR-2026-08-07-000001-v1/{TREE}x",
    f"renders/PALOMAR-2026-08-07-000001-v1/{TREE[:-1]}",
    # a name that is a prefix of a root only as a string, not by component
    f"renders/PALOMAR-2026-08-07-000001-v1/{TREE}extra/file",
]


@pytest.mark.parametrize("node", NODES)
def test_it_answers_exactly_as_the_scan_it_replaced(node):
    assert _reference_test(ROOTS)(node) == oracle(ROOTS, node), node


def test_an_empty_root_set_accepts_nothing():
    referenced = _reference_test(set())
    for node in NODES:
        assert referenced(node) is False
        assert oracle(set(), node) is False


def test_a_root_of_an_unexpected_depth_still_agrees():
    """Depths are taken from the roots rather than assumed, so a path shape no
    entry should ever produce still answers the same as the old scan."""
    roots = {"renders/one", f"renders/two/{TREE}/deeper"}
    for node in (
        "renders",
        "renders/one",
        "renders/one/inside",
        "renders/two",
        f"renders/two/{TREE}",
        f"renders/two/{TREE}/deeper",
        f"renders/two/{TREE}/deeper/inside",
        f"renders/two/{TREE}/elsewhere",
        "renders/three",
    ):
        assert _reference_test(roots)(node) == oracle(roots, node), node


def test_it_stops_scanning_every_root_for_every_node():
    """The property that made this quadratic: cost per node must not grow with
    the number of entries."""
    lookups = []

    class CountingSet(set):
        def __contains__(self, item):
            lookups.append(item)
            return set.__contains__(self, item)

    counts = {}
    for size in (2, 200):
        roots = CountingSet(
            f"renders/PALOMAR-2026-08-07-{index:06d}-v1/{TREE}" for index in range(size)
        )
        lookups.clear()
        _reference_test(roots)(f"renders/PALOMAR-2026-08-07-000000-v1/{TREE}/Challenge/index.html")
        counts[size] = len(lookups)
    assert counts[2] == counts[200], counts


def test_the_complete_boundary_reports_an_orphan_after_valid_ancestors(tmp_path):
    expected = f"renders/PALOMAR-2026-08-07-000001-v1/{TREE}"
    bundle = tmp_path / expected
    bundle.mkdir(parents=True)
    (bundle / "index.html").write_text("referenced\n", encoding="utf-8")
    (tmp_path / "renders/orphan.txt").write_text("orphan\n", encoding="utf-8")

    assert validate_bundle_references(tmp_path, {expected}, set()) == [
        "renders/orphan.txt: render artifact is not referenced by any entry"
    ]


def test_the_scoped_boundary_reads_only_arriving_nodes(tmp_path):
    renders = tmp_path / "renders"
    renders.mkdir()
    (renders / "historical-orphan.txt").write_text("old\n", encoding="utf-8")
    (renders / "arriving-orphan.txt").write_text("new\n", encoding="utf-8")

    assert validate_bundle_references(
        tmp_path,
        set(),
        set(),
        frozen_paths=frozenset({"renders/arriving-orphan.txt"}),
    ) == [
        "renders/arriving-orphan.txt: render artifact is not referenced by any entry"
    ]


@pytest.mark.parametrize("directory", ["renders", "evidence"])
def test_the_complete_boundary_rejects_a_symbolic_link_tree(tmp_path, directory):
    target = tmp_path / f"{directory}-target"
    target.mkdir()
    (tmp_path / directory).symlink_to(target, target_is_directory=True)

    assert validate_bundle_references(tmp_path, set(), set()) == [
        f"{directory}/: must be an ordinary directory, not a symbolic link"
    ]


@pytest.mark.parametrize("directory", ["renders", "evidence"])
def test_the_complete_boundary_rejects_a_non_directory_tree(tmp_path, directory):
    (tmp_path / directory).write_text("not a tree\n", encoding="utf-8")

    assert validate_bundle_references(tmp_path, set(), set()) == [
        f"{directory}/: must be a directory"
    ]
