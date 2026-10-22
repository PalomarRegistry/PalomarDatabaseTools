"""Direct tests for immutable render-bundle validation."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import shutil

import pytest
import render_validation


def _rehash_bundle(db, entry):
    bundle = db.path / entry["challenge_render"]["artifact_path"]
    files = [
        {"path": path.relative_to(bundle).as_posix(), "bytes": path.stat().st_size,
         "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in sorted(bundle.rglob("*"))
        if path.is_file() and path.name != "artifact-manifest.json"
    ]
    tree = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    (bundle / "artifact-manifest.json").write_text(json.dumps({
        "schema_version": 1, "artifact_tree_sha256": tree, "files": files,
    }))
    relative = f"renders/{entry['id']}-v{entry['version']}/{tree}/"
    destination = db.path / relative
    if bundle != destination:
        bundle.rename(destination)
    entry["challenge_render"].update(artifact_tree_sha256=tree, artifact_path=relative)
    return destination


def _owned_styles_entry(db):
    entry = db.entry_data("PALOMAR-2026-07-29-000001", 1)
    bundle = db.path / entry["challenge_render"]["artifact_path"]
    shutil.rmtree(bundle)
    fixture = Path(__file__).parent / "fixtures" / "renderer-owned-styles" / "bundle"
    shutil.copytree(fixture, bundle)
    return entry, _rehash_bundle(db, entry)


def test_renderer_owned_styles_validate_on_root_and_nested_pages(db):
    entry, bundle = _owned_styles_entry(db)
    page = (bundle / "Challenge/index.html").read_text()
    (bundle / "index.html").write_text(page.replace("../palomar-", "palomar-"))
    nested = bundle / "x/y/index.html"
    nested.parent.mkdir(parents=True)
    nested.write_text(page.replace("../palomar-", "../../palomar-"))
    _rehash_bundle(db, entry)
    assert render_validation.validate_render(db.path, entry, "entry.json") == []


@pytest.mark.parametrize("mutation,marker", [
    ("css", "stylesheet bytes do not match"),
    ("palette", "stylesheet bytes do not match"),
    ("extra-css", "only the renderer-owned CSS file"),
    ("extra-style", "one renderer-owned head style"),
    ("link", "untrusted stylesheet link"),
    ("body-style", "one renderer-owned head style"),
    ("head-comment", "ambiguous HTML markup"),
    ("body-comment", "ambiguous HTML markup"),
    ("marked", "ambiguous HTML markup"),
    ("pi", "ambiguous HTML markup"),
    ("head-data", "stylesheet bytes do not match"),
    ("link-media", "untrusted stylesheet link"),
    ("upper-css", "only the renderer-owned CSS file"),
])
def test_rehashed_untrusted_styles_are_rejected(db, mutation, marker):
    entry, bundle = _owned_styles_entry(db)
    page_path = bundle / "Challenge/index.html"
    page = page_path.read_text()
    if mutation == "css":
        css = bundle / "palomar-render.css"
        css.write_text(css.read_text() + "body {display:none}")
    elif mutation == "palette":
        page = page.replace("--palomar-ink: #24292e", "--palomar-ink: #ffffff")
    elif mutation == "extra-css":
        (bundle / "evil.css").write_text("body {display:none}")
    elif mutation == "extra-style":
        page = page.replace("</head>", '<style id="palomar-declaration-style">body{display:none}</style></head>')
    elif mutation == "link":
        page = page.replace('href="../palomar-render.css"', 'href="../evil.css"')
    elif mutation == "body-style":
        start = page.index('<style id="palomar-declaration-style">')
        end = page.index("</style>", start) + len("</style>")
        style = page[start:end]
        page = (page[:start] + page[end:]).replace("</body>", style + "</body>")
    elif mutation == "head-comment":
        page = page.replace("</head>", '<!-- split --!><style>body{display:none}</style>--></head>')
    elif mutation == "body-comment":
        page = page.replace("</body>", "<!-- harmless --></body>")
    elif mutation == "marked":
        page = page.replace("</body>", "<![CDATA[<style>body{display:none}</style>]]></body>")
    elif mutation == "pi":
        page = page.replace("</body>", "<?probe?></body>")
    elif mutation == "head-data":
        page = page.replace("</head>", "<title>changed</title></head>")
    elif mutation == "link-media":
        page = page.replace('rel="stylesheet"', 'rel="stylesheet" media="print"')
    elif mutation == "upper-css":
        (bundle / "evil.CSS").write_text("body {display:none}")
    page_path.write_text(page)
    _rehash_bundle(db, entry)
    errors = render_validation.validate_render(db.path, entry, "entry.json")
    assert any(marker in error for error in errors), errors


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
