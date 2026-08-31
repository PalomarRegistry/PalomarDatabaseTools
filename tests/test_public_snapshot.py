"""The public release is the active part of the private database, copied.

Not projected. A record used to be rewritten on the way out, so its published
bytes were a function of publisher code rather than of the commit -- and that
code changed shape twice under a fixed record. The scores that made it worth
rewriting now live outside the record, in `scores/`, which is never staged.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.request
import xml.etree.ElementTree as ET

import pytest
import release_delta

from stage_public import stage_public


def _take_down(db, identifier, version):
    db.write_json(
        "takedowns.json",
        {
            "schema_version": 1,
            "takedowns": [
                {
                    "id": identifier,
                    "version": version,
                    "taken_down_at": "2026-08-06T12:00:00Z",
                    "authorized_by_login": "avigad",
                    "authorization_issue": 101,
                    "reason": "Private moderation reason that must never be published.",
                }
            ],
        },
    )


def test_snapshot_contains_every_public_artifact_for_active_entries(db, tmp_path):
    output = tmp_path / "release"
    written = stage_public(db.path, output)
    index = json.loads((output / "index.json").read_text())
    entry = db.read_json("entries/PALOMAR-2026-07-29-000001-v1.json")

    assert set(index) == {"schema_version", "entries"}
    assert index["schema_version"] == 3
    assert index["entries"] == db.summaries()
    assert output / index["entries"][0]["path"] in written
    assert (output / entry["challenge_render"]["artifact_path"]).is_dir()
    assert (output / entry["verification"]["evidence_path"]).is_dir()
    archived_review = json.loads(
        (output / entry["verification"]["evidence_path"] / "review.json").read_text()
    )
    assert "scores" not in archived_review
    assert all("scores" not in step for step in archived_review["checks"])
    assert (output / "schema-v3.json").is_file()
    assert not (output / "schema-v1.json").exists()
    assert (output / "LICENSE").is_file()
    assert (output / "feed.xml").is_file()
    assert (output / "release-delta.json").is_file()
    assert not (output / "takedowns.json").exists()
    assert not (output / "registrations").exists()
    delta = json.loads((output / "release-delta.json").read_text())
    assert not any(
        row["path"].startswith("registrations/")
        for group in ("additions", "stable", "aggregates")
        for row in delta[group]
    )


def test_the_whole_registry_index_is_staged_for_reconciliation_and_never_published(db, tmp_path):
    """It is a build intermediate now, not a served object.

    The builders consume the validated records already in memory. The file is
    still written into the staging directory solely to give the weekly
    whole-tree reconciliation an explicit active set. It is not in the delta,
    so nothing uploads it: a document whose size is the registry's, built,
    hashed, uploaded and read back once per accepted result, was the last O(S)
    term a publication paid.
    """
    for serial in range(2, 12):
        db.add_entry(f"PALOMAR-2026-07-29-{serial:06d}", 1)
    output = tmp_path / "release"
    stage_public(db.path, output)

    index = json.loads((output / "index.json").read_text())
    assert set(index) == {"schema_version", "entries"}
    assert index["entries"] == db.summaries()
    assert len(index["entries"]) == 11
    delta = json.loads((output / "release-delta.json").read_text())
    assert "index.json" not in [
        row["path"]
        for group in ("additions", "stable", "aggregates")
        for row in delta[group]
    ]


def test_a_publication_no_longer_uploads_a_copy_of_the_whole_registry(db, tmp_path):
    """The growth class, measured as the bytes a release writes under its own
    prefix, which is what it pays for on every accepted result.

    Counting objects would measure nothing: the index was one object whether
    the registry held two records or a hundred thousand, and its size was the
    whole point.
    """
    weighed = {}
    for size in (2, 20):
        for serial in range(2, size + 1):
            db.add_entry(f"PALOMAR-2026-07-29-{serial:06d}", 1)
        output = tmp_path / f"release{size}"
        stage_public(db.path, output)
        delta = json.loads((output / "release-delta.json").read_text())
        weighed[size] = sum(row["bytes"] for row in delta["aggregates"])
        for serial in range(2, size + 1):
            db.remove(f"entries/PALOMAR-2026-07-29-{serial:06d}-v1.json")
        db.reindex()

    assert weighed[2] == weighed[20], weighed


def _rewrite_review(db, change):
    """Put a review of some other shape where the staged one will be read."""
    entry = db.read_json("entries/PALOMAR-2026-07-29-000001-v1.json")
    review_path = db.path / entry["verification"]["evidence_path"] / "review.json"
    review = json.loads(review_path.read_text())
    change(review)
    review_path.write_text(json.dumps(review))


@pytest.mark.parametrize("field,value", [("scores", {"clarity": 4}), ("severity", "info")])
def test_snapshot_refuses_internal_review_fields_in_public_evidence(db, tmp_path, field, value):
    def change(review):
        if field == "scores":
            review[field] = value
        else:
            review["checks"] = [{"outcome": "neutral", "findings": [{field: value}]}]

    _rewrite_review(db, change)
    with pytest.raises(ValueError, match=f"review.*{field}, which no published review may carry"):
        stage_public(db.path, tmp_path / "release")


@pytest.mark.parametrize(
    "where,change",
    [
        (
            "review.confidence",
            lambda review: review.update({"confidence": 0.9}),
        ),
        (
            "review.checks[0].raw_score",
            lambda review: review.update(
                {"checks": [{"outcome": "neutral", "raw_score": 4, "findings": []}]}
            ),
        ),
        (
            "review.checks[0].findings[0].model_rationale",
            lambda review: review.update(
                {
                    "checks": [
                        {
                            "outcome": "neutral",
                            "findings": [
                                {
                                    "evidence": "e",
                                    "message": "m",
                                    "model_rationale": "why the model said so",
                                }
                            ],
                        }
                    ]
                }
            ),
        ),
    ],
)
def test_snapshot_refuses_a_review_field_nobody_has_named_yet(db, tmp_path, where, change):
    """The reason this is an allowlist.

    The check that named `scores` and `severity` answered "does this carry the
    two fields we remove", and a review contract that grows a third answers
    that question fine while serving it. Nothing here has ever seen these
    names, which is exactly the case.
    """
    _rewrite_review(db, change)
    with pytest.raises(ValueError, match=f"{re.escape(where)}, which no published review may carry"):
        stage_public(db.path, tmp_path / "release")


def test_snapshot_refuses_a_new_object_inside_a_field_that_is_allowed(db, tmp_path):
    """A new shape leaks as readily as a new name.

    `sources_checked` is a list of strings a pass may publish. As a list of
    objects, each with the score the pass gave that source, it satisfies every
    key check and carries the thing those checks exist to hold back.
    """
    _rewrite_review(
        db,
        lambda review: review.update(
            {
                "checks": [
                    {
                        "outcome": "neutral",
                        "sources_checked": [{"path": "README.md", "score": 2}],
                        "findings": [],
                    }
                ]
            }
        ),
    )
    with pytest.raises(ValueError, match="unexpected object at review.checks"):
        stage_public(db.path, tmp_path / "release")


@pytest.mark.parametrize(
    "value,complaint",
    [
        (4, r"carries int at review\.checks\[0\]\.summary"),
        ([["a rank", 2]], r"nests an array at review\.checks\[0\]\.summary"),
    ],
)
def test_snapshot_refuses_a_number_where_a_review_carries_text(db, tmp_path, value, complaint):
    """A number does not need a new field name to be a published score."""
    _rewrite_review(
        db,
        lambda review: review.update(
            {"checks": [{"outcome": "neutral", "summary": value, "findings": []}]}
        ),
    )
    with pytest.raises(ValueError, match=complaint):
        stage_public(db.path, tmp_path / "release")


def test_snapshot_publishes_a_review_carrying_everything_a_review_may_carry(db, tmp_path):
    """The other half: an allowlist that is too narrow fails real reviews.

    Every field the reviewer writes into a pass and a finding, on one record,
    so that narrowing this list breaks a test rather than a publication.
    """
    _rewrite_review(
        db,
        lambda review: review.update(
            {
                "checks": [
                    {
                        "step": "statement_alignment",
                        "outcome": "warning",
                        "summary": "What this pass concluded.",
                        "trust_level": "qualified",
                        "sources_checked": ["README.md"],
                        "codes_checked": ["arxiv:math.CO", "msc2020:52C10"],
                        "declarations_checked": ["Foo.bar"],
                        "findings": [{"evidence": "README.md:1", "message": "a remark"}],
                    }
                ]
            }
        ),
    )
    entry = db.read_json("entries/PALOMAR-2026-07-29-000001-v1.json")
    review_path = db.path / entry["verification"]["evidence_path"] / "review.json"
    entry["review"]["report"]["sha256"] = hashlib.sha256(review_path.read_bytes()).hexdigest()
    db.write_json("entries/PALOMAR-2026-07-29-000001-v1.json", entry)
    db.reindex()

    stage_public(db.path, tmp_path / "release")


def test_takedown_removes_record_and_artifacts_but_publishes_minimal_tombstone(db, tmp_path):
    identifier = "PALOMAR-2026-07-29-000001"
    entry = db.read_json(f"entries/{identifier}-v1.json")
    _take_down(db, identifier, 1)
    output = tmp_path / "release"

    stage_public(db.path, output)

    assert json.loads((output / "index.json").read_text())["entries"] == []
    assert not (output / f"entries/{identifier}-v1.json").exists()
    assert not (output / entry["challenge_render"]["artifact_path"]).exists()
    assert not (output / entry["verification"]["evidence_path"]).exists()
    tombstone = json.loads((output / f"tombstones/{identifier}-v1.json").read_text())
    assert tombstone == {
        "id": identifier,
        "version": 1,
        "taken_down_on": "2026-08-06",
    }
    # Not only the tombstone: the private reason and the authenticated
    # authorization record must be absent from every staged byte, because the
    # whole staged tree is what a release uploads.
    for path in sorted(output.rglob("*")):
        if not path.is_file():
            continue
        staged = path.read_bytes()
        assert b"Private moderation reason" not in staged, path
        assert b"authorized_by_login" not in staged, path
        assert b"authorization_issue" not in staged, path
    assert ET.parse(output / "feed.xml").findall("./channel/item") == []


def test_taking_down_latest_version_makes_previous_version_current(db, tmp_path):
    identifier = "PALOMAR-2026-07-29-000001"
    db.add_entry(identifier, 2)
    _take_down(db, identifier, 2)
    output = tmp_path / "release"

    stage_public(db.path, output)

    summaries = json.loads((output / "index.json").read_text())["entries"]
    assert [(row["id"], row["version"]) for row in summaries] == [(identifier, 1)]
    item = ET.parse(output / "feed.xml").find("./channel/item")
    assert item is not None
    assert "version=1" in item.findtext("guid")


def test_a_published_record_is_the_committed_file_byte_for_byte(db, tmp_path):
    """The property the projection made impossible.

    While the record was rewritten on the way out, its published bytes depended
    on publisher code, so the same commit could serve different bytes after a
    deploy -- which happened twice. Equal bytes is what lets a reader compare
    the two, lets the object be treated as immutable, and lets it be cached.
    """
    output = tmp_path / "release"
    stage_public(db.path, output)
    index = json.loads((output / "index.json").read_text())
    assert index["entries"], "no entries to check"
    for summary in index["entries"]:
        assert (output / summary["path"]).read_bytes() == (
            db.path / summary["path"]
        ).read_bytes(), summary["path"]


def test_the_scores_are_not_staged(db, tmp_path):
    """They are recorded so the decision stays reconstructable, and that is all.

    The same repository at the same commit has scored 5 and then 4 on the same
    axis across two runs of the same policy, so the numbers do not mean what a
    reader would take them to mean. The verdict they produced is stable, and is
    published.
    """
    output = tmp_path / "release"
    stage_public(db.path, output)

    assert (db.path / "scores").is_dir(), "the fixture records them"
    assert not (output / "scores").exists()
    assert not (output / "scores-v1.json").exists(), (
        "even the schema of what is not served stays out of the release"
    )
    for path in output.rglob("*.json"):
        if path.name in ("release-delta.json", "release-plan.json"):
            continue
        assert "statement_alignment" not in path.read_text(), path


def test_a_record_that_carries_scores_is_refused_rather_than_published(db, tmp_path):
    """With no projection left to do the removing, the schema is the guard.

    `review` is `additionalProperties: false`, so a record that smuggled the
    numbers back in fails the check staging already runs and is not published.
    A projection could only be trusted while every publisher agreed to apply
    it; this cannot be bypassed by forgetting.
    """
    entry = db.read_json("entries/PALOMAR-2026-07-29-000001-v1.json")
    entry["review"]["scores"] = {"clarity": 4}
    db.write_json("entries/PALOMAR-2026-07-29-000001-v1.json", entry)

    with pytest.raises(ValueError, match="fails schema-v3.json"):
        stage_public(db.path, tmp_path / "release")


def test_staging_reports_an_unevaluable_entry_schema_without_fetching(
    db, tmp_path, monkeypatch
):
    def forbid_network(*_args, **_kwargs):
        raise AssertionError("entry schema staging attempted network retrieval")

    monkeypatch.setattr(urllib.request, "urlopen", forbid_network)
    schema = db.read_json("schema-v3.json")
    schema["properties"]["unused_future_field"] = {
        "$ref": "https://example.invalid/hostile-entry-schema.json"
    }
    db.write_json(
        "schema-v3.json",
        schema,
    )

    with pytest.raises(ValueError, match="entry schema cannot be evaluated safely"):
        stage_public(db.path, tmp_path / "release")


def test_a_numerically_equal_float_entry_version_is_not_published(db, tmp_path):
    entry = db.read_json("entries/PALOMAR-2026-07-29-000001-v1.json")
    entry["schema_version"] = 3.0
    db.write_json("entries/PALOMAR-2026-07-29-000001-v1.json", entry)

    with pytest.raises(ValueError, match="unsupported schema_version"):
        stage_public(db.path, tmp_path / "release")


def test_every_immutable_schema_is_published_from_its_canonical_file(db, tmp_path):
    """Each record is checked against the immutable contract it declares.

    The reviewer built a record against the canonical schema; the release
    served a different document under the same name; a record could satisfy
    the contract and fail what was served beside it. One shape, one schema.
    """
    # Even an obsolete schema-shaped file cannot reopen public discovery.
    db.write_json("schema-v1.json", {"title": "obsolete pre-launch draft"})
    output = tmp_path / "release"
    stage_public(db.path, output)

    served = output / "schema-v3.json"
    assert served.read_bytes() == (db.path / "schema-v3.json").read_bytes()
    assert not (output / "schema-v1.json").exists()
    assert not (output / "canonical-schema-v3.json").exists()
    assert not (output / "public-schema-v3.json").exists()

    delta = json.loads((output / "release-delta.json").read_text())
    public_paths = {
        row["path"]
        for group in ("additions", "stable", "aggregates")
        for row in delta[group]
    }
    assert "schema-v3.json" in public_paths
    assert "schema-v1.json" not in public_paths


def test_snapshot_staging_never_builds_the_separately_owned_availability(db, tmp_path):
    """The mutable manifest has one owner, not a compatibility path through
    the snapshot stager that disables incremental publication."""
    output = tmp_path / "public"
    db.write_json("source-availability.json", {
        "schema_version": 1,
        "generated_at": "2026-08-07T00:00:00Z",
        "repositories": [],
    })
    stage_public(db.path, output)

    assert not (output / "source-availability.json").exists()
    delta = json.loads((output / "release-delta.json").read_text())
    assert delta["schema_version"] == release_delta.DELTA_SCHEMA
    paths = [
        row["path"]
        for group in ("additions", "stable", "aggregates")
        for row in delta[group]
    ]
    assert "source-availability.json" not in paths
    assert "release-delta.json" not in paths
    assert "recent.json" in paths, "what the landing page reads is part of a release"


def test_stage_cli_rejects_the_removed_availability_mode(db, tmp_path):
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "tools/stage_public.py",
            "--root",
            str(db.path),
            "--output",
            str(tmp_path / "public"),
            "--availability",
            str(tmp_path / "availability.json"),
        ],
        cwd=__import__("pathlib").Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "unrecognized arguments: --availability" in result.stderr
