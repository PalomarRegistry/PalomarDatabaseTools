"""Direct tests for immutable render-bundle validation."""

from __future__ import annotations

import copy
import shutil

import pytest
import render_validation


def test_the_canonical_bundle_passes_without_mutating_its_entry(db):
    entry = db.entry_data("PALOMAR-2026-07-29-000001", 1)
    before = copy.deepcopy(entry)

    assert render_validation.validate_render(db.path, entry, "entry.json") == []
    assert entry == before


def test_artifact_path_is_bound_before_the_bundle_is_read(db):
    entry = db.entry_data("PALOMAR-2026-07-29-000001", 1)
    tree_hash = entry["challenge_render"]["artifact_tree_sha256"]
    entry["challenge_render"]["artifact_path"] = "renders/somewhere-else/"

    assert render_validation.validate_render(db.path, entry, "entry.json") == [
        "entry.json:challenge_render.artifact_path: must be "
        f"renders/PALOMAR-2026-07-29-000001-v1/{tree_hash}/"
    ]


def test_missing_bundle_fails_at_the_render_boundary(db):
    entry = db.entry_data("PALOMAR-2026-07-29-000001", 1)
    shutil.rmtree(db.path / entry["challenge_render"]["artifact_path"])

    assert render_validation.validate_render(db.path, entry, "entry.json") == [
        "entry.json:challenge_render: artifact directory is missing or symbolic"
    ]


def test_malformed_render_shape_is_left_to_the_entry_schema(db):
    entry = db.entry_data("PALOMAR-2026-07-29-000001", 1)
    entry["challenge_render"] = []

    assert render_validation.validate_render(db.path, entry, "entry.json") == []


def test_file_cap_is_enforced_without_validation_orchestration(db, monkeypatch):
    entry = db.entry_data("PALOMAR-2026-07-29-000001", 1)
    page = (
        db.path
        / entry["challenge_render"]["artifact_path"]
        / "Challenge"
        / "index.html"
    )
    monkeypatch.setattr(
        render_validation, "MAX_RENDER_FILE_BYTES", page.stat().st_size - 1
    )

    errors = render_validation.validate_render(db.path, entry, "entry.json")

    assert any("artifact file exceeds the size cap" in error for error in errors)


@pytest.mark.parametrize(
    ("constant", "marker"),
    [
        ("MAX_RENDER_NODES", "filesystem-node cap"),
        ("MAX_RENDER_FILES", "file-count cap"),
        ("MAX_RENDER_BYTES", "total-size cap"),
    ],
)
def test_bundle_caps_are_enforced_at_the_render_boundary(
    db, monkeypatch, constant, marker
):
    entry = db.entry_data("PALOMAR-2026-07-29-000001", 1)
    monkeypatch.setattr(render_validation, constant, 0)

    errors = render_validation.validate_render(db.path, entry, "entry.json")

    assert any(marker in error for error in errors)
