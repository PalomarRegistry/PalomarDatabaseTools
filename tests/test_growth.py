"""What one publication costs, over two registries of different sizes.

A publication happens once per accepted result, so any term in it that grows
with the number of results costs the registry O(S²) over its life. This is the
measurement that says whether that is true, and it is deliberately about what a
publication *costs* rather than about how many times anything was called: the
shape being ruled out here is a constant number of calls that each read or
write a growing document, which counting calls would report as no change at all.

Four things are measured, because each of them was O(S) at some point in this
codebase's history and each was fixed by a different change: the entry files
opened, the bytes generated, the objects written, and the rows written into the
document that describes the release.

Eleven and ninety-one rather than ten and a hundred. Every count in these
documents is decimal, so a registry ten times larger writes one more digit;
without choosing the sizes, this would be asserting that ten times the registry
costs the same to within a byte, which is true and reads like an accident.
"""

from __future__ import annotations

import json

import pytest
import release_delta
import stage_public


def _entries_read(monkeypatch, root):
    """Which canonical entry files a staging run opens.

    Only the ones under the database's own `entries/`. The copy the release
    stages is opened too, to check it against the schema it publishes, but that
    is one file per record added and is the work the release exists to do.
    """
    opened: list[str] = []
    original = stage_public._load_json

    def counted(path):
        if path.parent == root / "entries":
            opened.append(path.name)
        return original(path)

    monkeypatch.setattr(stage_public, "_load_json", counted)
    return opened


def _generated(site):
    """Every byte this run produced, including the ones it never uploads."""
    return sum(path.stat().st_size for path in site.rglob("*") if path.is_file())


def _cost(repo, tmp_path, monkeypatch, size, day="2026-07-29"):
    # Both registries have to be over the caps, or the two capped pages are
    # still filling up and grow with the registry until they are. That is a
    # bound working, not a bound missing, but it is not what this measures.
    for module, name in (
        ("build_recent", "RECENT_ITEMS"),
        ("patch_surfaces", "RECENT_ITEMS"),
        ("build_subjects", "SUBJECT_PAGE_ITEMS"),
        ("patch_surfaces", "SUBJECT_PAGE_ITEMS"),
    ):
        monkeypatch.setattr(f"{module}.{name}", 3)
    monkeypatch.setattr("build_registration_lookups.REPOSITORY_LIMIT", 3)
    # Forty, so that both registries put the record being added at the same
    # offset in its word's open postings page and at a page number of the same
    # width. The open page is what a publication rewrites, so at the default
    # size the two runs would be comparing a page of twelve postings against a
    # page of ninety-two, which is a bound not yet reached rather than a cost
    # that grows. See tests/test_search.py for the same measurement made where
    # the sizes were chosen for it.
    monkeypatch.setattr("build_search.PAGE_SIZE", 40)
    served = tmp_path / f"served{size}"
    served.mkdir()
    # Grow the registry on another contiguous day, while keeping the touched
    # day's state identical. Total-registry and same-day page growth are
    # distinct cost terms; this test measures only the former.
    for serial in range(1, size):
        repo.add_entry(f"PALOMAR-2026-07-28-{serial:06d}", 1)
    repo.commit(f"a registry of {size}")
    base = tmp_path / f"base{size}"
    stage_public.stage_public(repo.path, base)
    import shutil

    shutil.copytree(base, served, dirs_exist_ok=True)
    previous = release_delta.parse((base / "release-delta.json").read_bytes())

    newcomer = repo.next_identifier(day)
    repo.add_entry(newcomer, 1)
    repo.commit("one more")
    site = tmp_path / f"next{size}"
    opened = _entries_read(monkeypatch, repo.path)
    stage_public.stage_public(
        repo.path,
        site,
        previous=release_delta.base_of(previous),
        prior=served,
    )
    delta = release_delta.parse((site / "release-delta.json").read_bytes())
    monkeypatch.undo()

    repo.remove(f"entries/{newcomer}-v1.json")
    repo.reindex()
    return {
        "entry files read": len(opened),
        "bytes generated": _generated(site),
        "objects written": sum(
            1 for path in site.rglob("*") if path.is_file()
        ),
        "delta rows": sum(
            len(delta[group])
            for group in ("additions", "withdrawals", "stable", "aggregates")
        ),
        "delta rows for pages": len(delta["stable"]),
        "delta rows for postings": sum(
            1 for row in delta["stable"] if row["path"].startswith("search/")
        ),
    }


def test_adding_one_result_costs_the_same_after_every_surface_is_bounded(
    repo, tmp_path, monkeypatch
):
    small = _cost(repo, tmp_path, monkeypatch, 11)
    large = _cost(repo, tmp_path, monkeypatch, 91)
    assert small == large, {"11": small, "91": large}


def test_a_publication_opens_only_the_records_it_touches(repo, tmp_path, monkeypatch):
    """Zero, in fact: what the release adds is copied rather than parsed by the
    stager, and the pages it lands on come from what is being served."""
    small = _cost(repo, tmp_path, monkeypatch, 11)
    assert small["entry files read"] == 1, "it read a record it did not touch"


def test_the_delta_does_not_carry_a_row_per_published_object(repo, tmp_path, monkeypatch):
    """`stable` used to name every browse page, subject page and version index
    in the dataset, on a document written once per accepted result. That is the
    same O(S²) the delta exists to remove, inside the document that removes it.
    """
    small = _cost(repo, tmp_path, monkeypatch, 11)
    large = _cost(repo, tmp_path, monkeypatch, 91)
    assert small["delta rows for pages"] == large["delta rows for pages"]
    # One browse page and the two documents over it, the same three for each of
    # the two codes the record carries, its version index, the recent page and
    # the three feeds those two render into, plus the repository and exact
    # registration-identity lookups. The rest of the delta is the record
    # itself, its render and its evidence, which is the work.
    assert small["delta rows for pages"] - small["delta rows for postings"] == 16, small
    # Two per distinct word the fixture record carries, plus the fixed stopword
    # document that is deliberately staged and rewritten on every release.
    assert small["delta rows for postings"] == 2 * 34 + 1, small


def test_a_full_rebuild_is_the_one_place_the_cost_is_the_registry(repo, tmp_path):
    """Stated rather than assumed. A rebuild is what every uncertainty falls
    back to, so it has to be correct; it does not have to be cheap, and knowing
    which of the two paths is which is the point."""
    weighed = {}
    previous_size = 1
    for size in (11, 91):
        for serial in range(previous_size + 1, size + 1):
            repo.add_entry(f"PALOMAR-2026-07-29-{serial:06d}", 1)
        repo.commit(f"a registry of {size}")
        site = tmp_path / f"whole{size}"
        stage_public.stage_public(repo.path, site, full=True)
        weighed[size] = _generated(site)
        previous_size = size
    assert weighed[91] > weighed[11] * 4, weighed
