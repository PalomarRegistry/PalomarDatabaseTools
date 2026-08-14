"""What is new, as a bounded page rather than the registry sorted in a browser.

The landing page used to read `index.json` and every record it named. That is
the whole registry for a list of a couple of hundred rows, and it cost more
every time anybody else published anything. `recent.json` is that list, decided
by the same module the feeds and the subject pages read so that the three
cannot come to name different newest results.
"""

from __future__ import annotations

import copy
import json
import pathlib

import pytest

import release_delta
from build_recent import (
    RECENT_ITEMS,
    build_recent,
    render_hash,
    render_page,
    row,
    validate_entry_schema_for_recent,
    validate_recent,
    validate_recent_renders,
)
from stage_public import stage_public

FIRST = "PALOMAR-2026-07-29-000001"


def _recent(site):
    return json.loads((site / "recent.json").read_text())


def _renders(site):
    return json.loads((site / "recent-renders.json").read_text())


def _stage(root, site, **arguments):
    stage_public(root, site, **arguments)
    return release_delta.parse((site / "release-delta.json").read_bytes())


def _dated(db, identifier, stamp, version=1):
    """A record with a registration instant of its own, which is what orders it."""
    entry = db.entry_data(identifier, version, registered_at=stamp)
    db.install_entry(entry)
    return entry


def test_the_page_names_the_current_versions_newest_first(db, tmp_path):
    for serial in range(2, 5):
        _dated(db, f"PALOMAR-2026-07-29-{serial:06d}", f"2026-08-0{serial}T00:00:00Z")
    site = tmp_path / "release"
    stage_public(db.path, site)

    page = _recent(site)
    assert page["schema_version"] == 1
    assert [row["id"] for row in page["entries"]][:3] == [
        "PALOMAR-2026-07-29-000004",
        "PALOMAR-2026-07-29-000003",
        "PALOMAR-2026-07-29-000002",
    ]


def test_the_order_is_when_a_version_was_registered_and_not_when_it_was_reviewed(db, tmp_path):
    """The failure this page had, stated as two results.

    A review's verdict and the registration it leads to are different moments.
    Nothing is registered until the submitter has read their review and
    consented, and they may take as long as they like, so one submitter's
    review can be older than another's while their registration is newer.
    Ordering by the review put the result registered later behind the one
    registered first, and at a couple of hundred registrations a day that gap
    is a newly registered result ordered behind two hundred older ones and so
    absent from this page altogether: registered, and invisible on the landing
    page.
    """
    held = db.entry_data(
        "PALOMAR-2026-08-09-000001",
        1,
        accepted_at="2026-08-09",
        registered_at="2026-08-09T09:00:00Z",
    )
    held["review"]["reviewed_at"] = "2026-08-01T09:00:00Z"
    db.install_entry(held)
    prompt = db.entry_data(
        "PALOMAR-2026-08-08-000001",
        1,
        accepted_at="2026-08-08",
        registered_at="2026-08-08T09:00:00Z",
    )
    prompt["review"]["reviewed_at"] = "2026-08-07T09:00:00Z"
    db.install_entry(prompt)
    site = tmp_path / "release"
    stage_public(db.path, site)

    page = _recent(site)["entries"]
    assert [row["id"] for row in page][:2] == [
        "PALOMAR-2026-08-09-000001",
        "PALOMAR-2026-08-08-000001",
    ]
    assert page[0]["published_at"] == "2026-08-09T09:00:00Z"


def test_a_new_version_sorts_as_news_rather_than_beside_its_first_version(db, tmp_path):
    """A v2 is a new registration, so it is new here even though its result is
    not. Ordering on the result's date would file it among the results
    registered in the year of its v1, where nobody looking at what is new would
    see it."""
    db.add_entry("PALOMAR-2026-07-29-000002", 1, registered_at="2026-07-29T10:00:00Z")
    db.add_entry(FIRST, 2, registered_at="2027-01-01T10:00:00Z")
    site = tmp_path / "release"
    stage_public(db.path, site)

    page = _recent(site)["entries"]
    assert page[0]["id"] == FIRST
    assert page[0]["version"] == 2
    assert page[0]["published_at"] == "2027-01-01T10:00:00Z"


def test_only_the_current_version_of_a_result_appears(db, tmp_path):
    db.add_entry(FIRST, 2)
    site = tmp_path / "release"
    stage_public(db.path, site)

    rows = [row for row in _recent(site)["entries"] if row["id"] == FIRST]
    assert [row["version"] for row in rows] == [2]


def test_a_row_is_the_complete_landing_card_projection(db, tmp_path):
    """One bounded page is sufficient; rendering it needs no record requests."""
    db.add_entry(FIRST, 2)
    site = tmp_path / "release"
    stage_public(db.path, site)

    projected = next(item for item in _recent(site)["entries"] if item["id"] == FIRST)
    canonical = db.read_json(f"entries/{FIRST}-v2.json")
    source_mapping = next(
        mapping
        for mapping in canonical["preservation"]["repositories"]
        if mapping["source_repository"].casefold()
        == canonical["source"]["repository"].casefold()
        and mapping["commit"] == canonical["source"]["commit"]
    )
    assert projected == {
        "id": FIRST,
        "version": 2,
        "status": canonical["status"],
        "title": canonical["title"],
        "path": f"entries/{FIRST}-v2.json",
        "abstract": canonical["abstract"],
        "authors": [{"name": author["name"]} for author in canonical["authors"]],
        "classification": canonical["classification"],
        "formalization": {"theorem_names": canonical["formalization"]["theorem_names"]},
        "trust": {"level": canonical["trust"]["level"]},
        "source": {
            "repository": canonical["source"]["repository"],
            "commit": canonical["source"]["commit"],
            "project_path": canonical["source"].get("project_path"),
        },
        "preservation": {
            "repositories": [
                {
                    "source_repository": source_mapping["source_repository"],
                    "commit": source_mapping["commit"],
                    "fork_repository": source_mapping["fork_repository"],
                }
            ]
        },
        "published_at": canonical["registered_at"],
        "versions": 2,
    }


def test_the_projection_has_one_exact_checked_fixture():
    root = pathlib.Path(__file__).resolve().parents[1]
    entry = json.loads((root / "tests/fixtures/entry.json").read_text())
    expected = json.loads((root / "tests/fixtures/recent.json").read_text())

    document = {"schema_version": 1, "entries": [row(entry, 3)]}

    assert validate_recent(document) == expected


def test_the_render_projection_has_one_exact_checked_fixture():
    root = pathlib.Path(__file__).resolve().parents[1]
    entry = json.loads((root / "tests/fixtures/entry.json").read_text())
    expected = json.loads((root / "tests/fixtures/recent-renders.json").read_text())

    page = [row(entry, 3)]
    document = {
        "schema_version": 1,
        "renders": render_page(page, {(entry["id"], entry["version"]): render_hash(entry)}),
    }

    assert validate_recent_renders(document) == expected


def test_the_two_documents_name_the_same_results(db, tmp_path):
    """Built from one selection, so a reader that has the page has the hash of
    every result on it, and of nothing else."""
    for serial in range(2, 5):
        _dated(db, f"PALOMAR-2026-07-29-{serial:06d}", f"2026-08-0{serial}T00:00:00Z")
    site = tmp_path / "release"
    stage_public(db.path, site)

    page = _recent(site)
    renders = _renders(site)

    assert renders["schema_version"] == 1
    assert {(item["id"], item["version"]) for item in renders["renders"]} == {
        (item["id"], item["version"]) for item in page["entries"]
    }


def test_the_render_document_is_in_increasing_identifier_order(db, tmp_path):
    for serial in range(2, 5):
        _dated(db, f"PALOMAR-2026-07-29-{serial:06d}", f"2026-08-0{serial}T00:00:00Z")
    site = tmp_path / "release"
    stage_public(db.path, site)

    identifiers = [item["id"] for item in _renders(site)["renders"]]

    assert identifiers == sorted(identifiers)


def test_the_render_projection_refuses_a_row_it_cannot_address(db, tmp_path):
    """Dropping the row instead would publish a page whose hover does nothing
    for one result, with nothing anywhere saying why."""
    site = tmp_path / "release"
    stage_public(db.path, site)
    page = _recent(site)["entries"]

    with pytest.raises(ValueError, match="no render hash for"):
        render_page(page, {})


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda document: document.update({"entries": []}), "invalid shape"),
        (lambda document: document.update({"schema_version": True}), "unsupported schema version"),
        (
            lambda document: document["renders"][0].update({"artifact_path": "renders/"}),
            "invalid shape",
        ),
        (
            lambda document: document["renders"][0].update({"artifact_tree_sha256": "0" * 63}),
            "artifact_tree_sha256 is malformed",
        ),
        (lambda document: document["renders"][0].update({"id": "PALOMAR-1"}), "id is malformed"),
        (lambda document: document["renders"][0].update({"version": 0}), "version is invalid"),
    ],
)
def test_the_render_projection_refuses_unknown_or_malformed_shapes(mutate, reason):
    root = pathlib.Path(__file__).resolve().parents[1]
    document = json.loads((root / "tests/fixtures/recent-renders.json").read_text())
    mutate(document)

    with pytest.raises(ValueError, match=reason):
        validate_recent_renders(document)


def test_the_render_projection_refuses_duplicate_and_out_of_order_rows(db, tmp_path):
    _dated(db, "PALOMAR-2026-07-29-000002", "2026-08-02T00:00:00Z")
    site = tmp_path / "release"
    stage_public(db.path, site)
    document = _renders(site)

    duplicated = copy.deepcopy(document)
    duplicated["renders"].insert(1, copy.deepcopy(duplicated["renders"][0]))
    with pytest.raises(ValueError, match="increasing identifier order"):
        validate_recent_renders(duplicated)

    reversed_rows = copy.deepcopy(document)
    reversed_rows["renders"].reverse()
    with pytest.raises(ValueError, match="increasing identifier order"):
        validate_recent_renders(reversed_rows)


def test_the_render_projection_refuses_cap_plus_one_rows():
    root = pathlib.Path(__file__).resolve().parents[1]
    document = json.loads((root / "tests/fixtures/recent-renders.json").read_text())
    template = document["renders"][0]
    document["renders"] = [
        {**template, "id": f"PALOMAR-2026-07-29-{serial:06d}"}
        for serial in range(RECENT_ITEMS + 1)
    ]

    with pytest.raises(ValueError, match="200-row bound"):
        validate_recent_renders(document)


def test_the_render_document_sits_at_a_stable_key(db, tmp_path):
    """Rewritten in place beside the page it accompanies, not under a release
    prefix; the two are written together or not at all."""
    site = tmp_path / "release"
    delta = _stage(db.path, site)

    assert "recent-renders.json" in [row["path"] for row in delta["stable"]]
    assert "recent-renders.json" not in [row["path"] for row in delta["aggregates"]]


def test_every_canonical_schema_requires_the_fields_the_projection_reads():
    root = pathlib.Path(__file__).resolve().parents[1]
    path = root / "schema-v2.json"
    validate_entry_schema_for_recent(json.loads(path.read_text()), path.name)


def test_a_schema_cannot_quietly_make_a_projected_field_optional():
    root = pathlib.Path(__file__).resolve().parents[1]
    schema = json.loads((root / "schema-v2.json").read_text())
    schema["properties"]["formalization"]["required"].remove("theorem_names")

    with pytest.raises(ValueError, match="formalization does not require.*theorem_names"):
        validate_entry_schema_for_recent(schema, "schema-v2.json")


def test_the_entry_schema_must_require_preservation_for_recent_cards():
    root = pathlib.Path(__file__).resolve().parents[1]
    schema = json.loads((root / "schema-v2.json").read_text())
    schema["required"].remove("preservation")

    with pytest.raises(ValueError, match="entry does not require.*preservation"):
        validate_entry_schema_for_recent(schema, "schema-v2.json")


def test_the_projection_refuses_cap_plus_one_rows():
    root = pathlib.Path(__file__).resolve().parents[1]
    document = json.loads((root / "tests/fixtures/recent.json").read_text())
    document["entries"] = document["entries"] * (RECENT_ITEMS + 1)

    with pytest.raises(ValueError, match="200-row bound"):
        validate_recent(document)


def test_the_projection_refuses_a_boolean_schema_version():
    root = pathlib.Path(__file__).resolve().parents[1]
    document = json.loads((root / "tests/fixtures/recent.json").read_text())
    document["schema_version"] = True

    with pytest.raises(ValueError, match="unsupported schema version"):
        validate_recent(document)


def test_the_projection_refuses_a_path_that_disagrees_with_identity():
    root = pathlib.Path(__file__).resolve().parents[1]
    document = json.loads((root / "tests/fixtures/recent.json").read_text())
    document["entries"][0]["path"] = "entries/PALOMAR-2026-08-05-419273-v2.json"

    with pytest.raises(ValueError, match="path does not name its entry"):
        validate_recent(document)


def test_the_projection_refuses_a_preservation_mapping_for_another_source(db, tmp_path):
    db.add_entry(FIRST, 2)
    site = tmp_path / "release"
    stage_public(db.path, site)
    document = _recent(site)
    current = next(item for item in document["entries"] if item["id"] == FIRST)
    current["preservation"]["repositories"][0]["commit"] = "0" * 40

    with pytest.raises(ValueError, match="preservation does not match source"):
        validate_recent(document)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda document: document.update({"legacy_entries": []}),
        lambda document: document["entries"][0].update({"registered_at": "2026-01-01"}),
        lambda document: document["entries"][0]["authors"][0].update({"github": "somebody"}),
        lambda document: document["entries"][0]["source"].pop("project_path"),
        lambda document: document["entries"][0].update({"versions": 0}),
        lambda document: document["entries"][0].update({"preservation": None}),
        lambda document: document["entries"][0].update(
            {"preservation": {"repositories": []}}
        ),
    ],
)
def test_the_projection_refuses_unknown_or_malformed_shapes(db, tmp_path, mutate):
    site = tmp_path / "release"
    stage_public(db.path, site)
    document = _recent(site)
    mutate(document)

    with pytest.raises(ValueError):
        validate_recent(document)


def test_the_projection_refuses_duplicate_and_out_of_order_rows(db, tmp_path):
    _dated(db, "PALOMAR-2026-07-29-000002", "2026-08-02T00:00:00Z")
    site = tmp_path / "release"
    stage_public(db.path, site)
    document = _recent(site)

    duplicated = copy.deepcopy(document)
    duplicated["entries"].append(copy.deepcopy(duplicated["entries"][0]))
    with pytest.raises(ValueError, match="duplicated"):
        validate_recent(duplicated)

    reversed_page = copy.deepcopy(document)
    reversed_page["entries"].reverse()
    with pytest.raises(ValueError, match="newest first"):
        validate_recent(reversed_page)


def test_the_cap_is_a_hard_bound_and_keeps_the_newest(db, tmp_path, monkeypatch):
    """Unbounded this is `index.json` again under another name: the one served
    object whose size is the registry's. Dropping the newest instead would make
    the cap worse than having no page."""
    monkeypatch.setattr("build_recent.RECENT_ITEMS", 2)
    for serial in range(2, 8):
        _dated(db, f"PALOMAR-2026-07-29-{serial:06d}", f"2026-08-0{serial}T00:00:00Z")
    site = tmp_path / "release"
    stage_public(db.path, site)

    page = _recent(site)["entries"]
    assert len(page) == 2
    assert [row["id"] for row in page] == [
        "PALOMAR-2026-07-29-000007",
        "PALOMAR-2026-07-29-000006",
    ]


def test_the_page_and_the_main_feed_name_the_same_results_in_the_same_order(db, tmp_path):
    """The reason the selection is one module and not three. A landing page and
    a feed that disagreed about what is newest would each be defensible on their
    own terms, and a reader would have no way to tell which one was the
    registry."""
    import xml.etree.ElementTree as ET

    for serial in range(2, 6):
        _dated(db, f"PALOMAR-2026-07-29-{serial:06d}", f"2026-08-0{serial}T00:00:00Z")
    site = tmp_path / "release"
    stage_public(db.path, site)

    feed = ET.parse(site / "feed.xml")
    links = [item.findtext("link") for item in feed.findall("./channel/item")]
    page = [
        f"https://palomar-registry.org/entry.html?id={row['id']}&version={row['version']}"
        for row in _recent(site)["entries"]
    ]
    assert page == links


def test_the_cap_is_the_one_the_main_feed_uses():
    """Two numbers for one product decision is how a page and its feed come to
    show different amounts of the same thing."""
    from build_feeds import MAIN_FEED_ITEMS

    assert RECENT_ITEMS == MAIN_FEED_ITEMS == 200


def test_a_withdrawn_result_leaves_the_page(db, tmp_path):
    db.write_json(
        "takedowns.json",
        {
            "schema_version": 1,
            "takedowns": [
                {
                    "id": FIRST,
                    "version": 1,
                    "taken_down_at": "2026-08-06T12:00:00Z",
                    "authorized_by_login": "avigad",
                    "authorization_issue": 101,
                    "reason": "a private maintainer reason",
                }
            ],
        },
    )
    site = tmp_path / "release"
    stage_public(db.path, site)

    assert _recent(site)["entries"] == []


def test_the_page_sits_at_a_stable_key(db, tmp_path):
    """So a release that stages it rewrites the stable key rather than copying
    it forward under a fresh release prefix, which is what the feeds already do
    and for the same reason."""
    site = tmp_path / "release"
    delta = _stage(db.path, site)

    assert "recent.json" in [row["path"] for row in delta["stable"]]
    assert "recent.json" not in [row["path"] for row in delta["aggregates"]]


def test_what_the_page_weighs_does_not_grow_with_the_registry(repo, tmp_path, monkeypatch):
    """The growth class, measured as the bytes the publisher uploads for this
    object. A landing surface derived from the whole active set is exactly the
    thing being removed, so a page that quietly kept growing would put it
    back."""
    monkeypatch.setattr("build_recent.RECENT_ITEMS", 2)
    weighed = {}
    for size in (2, 20):
        for serial in range(10, 10 + size):
            repo.add_entry(f"PALOMAR-2026-07-29-{serial:06d}", 1)
        repo.commit(f"a registry of {size}")
        site = tmp_path / f"base{size}"
        delta = _stage(repo.path, site)
        weighed[size] = next(
            row["bytes"] for row in delta["stable"] if row["path"] == "recent.json"
        )
        for serial in range(10, 10 + size):
            repo.remove(f"entries/PALOMAR-2026-07-29-{serial:06d}-v1.json")
        repo.reindex()

    assert weighed[2] == weighed[20], weighed


def test_build_recent_needs_no_reads_of_its_own(db, tmp_path):
    """It is handed the records the stager already read. A surface that opened
    every record again would put back a whole pass over the registry."""
    summaries = db.summaries()
    entries = [db.read_json(summary["path"]) for summary in summaries]
    output = tmp_path / "site"
    output.mkdir()

    page, renders = build_recent(output, entries)

    assert page.relative_to(output).as_posix() == "recent.json"
    assert renders.relative_to(output).as_posix() == "recent-renders.json"
    assert [row["id"] for row in json.loads(page.read_text())["entries"]] == [FIRST]
    assert [row["id"] for row in json.loads(renders.read_text())["renders"]] == [FIRST]
