"""Publication writes what changed, and takes nothing away without a reason.

Keying every object by the release meant one new entry rewrote the whole
dataset. The tests that matter most here are the ones that would notice that
coming back: what a second publication writes, and what it leaves alone.
"""

from __future__ import annotations

import hashlib
import io
import json
import pathlib

import pytest
import release_delta
from publish_snapshot import (
    POINTER_KEY,
    _delete_keys,
    _ordering_path,
    _rank,
    audit,
    classify,
    publish_snapshot,
    reconcile,
)

TREE = "a" * 64
EVIDENCE = "b" * 64
REPLACEMENT_EVIDENCE = "c" * 64


def _identifier(serial: int) -> str:
    return f"PALOMAR-2026-08-07-{serial:06d}-v1"


def _day(entry: str) -> str:
    return "-".join(entry.split("-")[1:4])


def _page(entry: str) -> int:
    return (int(entry.split("-")[4]) - 1) // 200 + 1


class Missing(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class Taken(Exception):
    """What R2 answers to `If-None-Match: *` when the key already exists."""

    response = {"Error": {"Code": "PreconditionFailed"}}


class MemoryR2:
    """Enough of R2 to be wrong in the ways R2 is wrong."""

    def __init__(self, delete_errors: dict[str, str] | None = None):
        self.objects: dict[str, dict] = {}
        self.put_order: list[str] = []
        self.refused: list[str] = []
        self.delete_errors = delete_errors or {}
        self.delete_calls: list[list[str]] = []
        self.delete_order: list[str] = []
        self.page_size = 1000

    def head_object(self, *, Bucket, Key):
        del Bucket
        if Key not in self.objects:
            raise Missing()
        item = self.objects[Key]
        return {"ContentLength": len(item["Body"]), "Metadata": item["Metadata"]}

    def put_object(self, *, Bucket, Key, Body, Metadata, IfNoneMatch=None, **options):
        del Bucket
        # A conditional write is refused, not silently ignored. Without this
        # the fake would accept `create_only` writes as ordinary overwrites,
        # and every test of the guarantee would pass for the wrong reason.
        if IfNoneMatch == "*" and Key in self.objects:
            self.refused.append(Key)
            raise Taken()
        self.put_order.append(Key)
        self.objects[Key] = {"Body": bytes(Body), "Metadata": Metadata, **options}

    def get_object(self, *, Bucket, Key):
        del Bucket
        if Key not in self.objects:
            raise Missing()
        item = self.objects[Key]
        return {"Body": io.BytesIO(item["Body"]), "Metadata": item["Metadata"]}

    def list_objects_v2(self, *, Bucket, Prefix, ContinuationToken=None, **_options):
        del Bucket
        matching = sorted(key for key in self.objects if key.startswith(Prefix))
        start = matching.index(ContinuationToken) if ContinuationToken else 0
        page = matching[start : start + self.page_size]
        truncated = start + self.page_size < len(matching)
        response = {
            "IsTruncated": truncated,
            "Contents": [
                {"Key": key, "Size": len(self.objects[key]["Body"])} for key in page
            ],
        }
        if truncated:
            response["NextContinuationToken"] = matching[start + self.page_size]
        return response

    def delete_objects(self, *, Bucket, Delete):
        del Bucket
        errors = []
        self.delete_calls.append([row["Key"] for row in Delete["Objects"]])
        for row in Delete["Objects"]:
            key = row["Key"]
            self.delete_order.append(key)
            if key in self.delete_errors:
                errors.append({"Key": key, "Code": "AccessDenied",
                               "Message": self.delete_errors[key]})
                continue
            self.objects.pop(key, None)
        return {"Errors": errors} if errors else {}

    def keys(self, prefix: str) -> set[str]:
        return {key for key in self.objects if key.startswith(prefix)}


def build_site(
    root: pathlib.Path,
    name: str,
    entries: list[str],
    taken_down: list[str] | None = None,
    availability: bool = False,
    categories: int = 2,
) -> pathlib.Path:
    """A staged public tree shaped like the real one."""
    taken_down = taken_down or []
    site = root / name
    site.mkdir(parents=True)
    files: dict[str, str] = {}
    active = [entry for entry in entries if entry not in taken_down]
    for entry in active:
        files[f"entries/{entry}.json"] = json.dumps({"id": entry}) + "\n"
        files[f"renders/{entry}/{TREE}/Challenge/index.html"] = f"<html>{entry}</html>\n"
        files[f"evidence/{entry}/{EVIDENCE}/review.json"] = json.dumps({"for": entry}) + "\n"
    for entry in taken_down:
        files[f"tombstones/{entry}.json"] = json.dumps({"id": entry}) + "\n"
    files["LICENSE"] = "licence\n"
    # A feed per code, most of which no publication touches. The registry has
    # a few thousand of these and they used to be copied forward every time.
    files["feed.xml"] = "<rss>" + ",".join(active) + "</rss>\n"
    for number in range(categories):
        files[f"feeds/msc/{number:02d}A05.xml"] = f"<rss>{number}</rss>\n"
    # A browse page per record, the year document naming its day, and the head
    # naming the year. What matters here is only that each is written after
    # every page it points at.
    for entry in active:
        files[f"browse/{_day(entry)}/{_page(entry)}.json"] = (
            json.dumps({"entries": [entry]}) + "\n"
        )
        files[f"browse/{_day(entry)[:4]}.json"] = json.dumps({"days": [_day(entry)]}) + "\n"
    files["browse/index.json"] = json.dumps(
        {"years": sorted({_day(entry)[:4] for entry in active})}, sort_keys=True
    ) + "\n"
    files["index.json"] = json.dumps({"entries": active}, sort_keys=True) + "\n"
    if availability:
        # A legacy or hostile staging tree may carry this path, but the snapshot
        # publisher still must not accept it into the release it owns.
        files["source-availability.json"] = json.dumps({"schema_version": 1}) + "\n"

    immutable, stable, aggregates = [], [], []
    for relative, text in sorted(files.items()):
        path = site / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if relative == "source-availability.json":
            continue
        data = path.read_bytes()
        row = {
            "path": relative,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        if relative.startswith(("entries/", "renders/", "evidence/")):
            immutable.append(row)
        elif relative == "feed.xml" or relative.startswith(
            ("browse/", "feeds/", "tombstones/")
        ):
            stable.append(row)
        else:
            aggregates.append(row)

    delta = {
        "schema_version": release_delta.DELTA_SCHEMA,
        "surfaces": release_delta.SURFACES,
        "parent": None,
        "database_commit": "0" * 40,
        "additions": immutable,
        "withdrawals": [],
        "retired": [],
        "stable": stable,
        "aggregates": aggregates,
        "takedowns_git_blob": "f" * 40,
        "records": {"count": len(active), "root": release_delta.root_of(immutable)},
    }
    (site / "release-delta.json").write_bytes(release_delta.canonical_bytes(delta))
    return site


def stage_stable_documents(site: pathlib.Path, paths: list[str]) -> None:
    """Add real stable documents to one staged release fixture."""
    delta = release_delta.parse((site / "release-delta.json").read_bytes())
    for relative in paths:
        path = site / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"path": relative}) + "\n", encoding="utf-8")
        data = path.read_bytes()
        delta["stable"].append(
            {
                "path": relative,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    delta["stable"].sort(key=lambda row: row["path"])
    (site / "release-delta.json").write_bytes(release_delta.canonical_bytes(delta))


def prepare_record_replacement(
    root: pathlib.Path, site: pathlib.Path, entry: str, old_entry: bytes
) -> pathlib.Path:
    """Rewrite one fixture record and bind both sides in the migration shape."""
    entry_path = site / "entries" / f"{entry}.json"
    entry_path.write_text(json.dumps({"id": entry, "review": "no-problems"}) + "\n")
    old_evidence = site / "evidence" / entry / EVIDENCE
    new_evidence = site / "evidence" / entry / REPLACEMENT_EVIDENCE
    old_evidence.rename(new_evidence)
    review_path = new_evidence / "review.json"
    old_review = review_path.read_bytes()
    review_path.write_text(json.dumps({"for": entry, "outcome": "neutral"}) + "\n")

    delta = release_delta.parse((site / "release-delta.json").read_bytes())
    immutable = []
    for path in sorted(
        candidate
        for prefix in ("entries", "renders", "evidence")
        for candidate in (site / prefix).rglob("*")
        if candidate.is_file()
    ):
        data = path.read_bytes()
        immutable.append(
            {
                "path": path.relative_to(site).as_posix(),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    delta["additions"] = immutable
    delta["records"]["root"] = release_delta.root_of(immutable)
    (site / "release-delta.json").write_bytes(release_delta.canonical_bytes(delta))

    manifest = root / "record-replacements.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "migration": "review-language-v3",
                "database_changes": [
                    {
                        "entry": f"entries/{entry}.json",
                        "old_entry_sha256": hashlib.sha256(old_entry).hexdigest(),
                        "new_entry_sha256": hashlib.sha256(entry_path.read_bytes()).hexdigest(),
                        "old_review_sha256": hashlib.sha256(old_review).hexdigest(),
                        "new_review_sha256": hashlib.sha256(review_path.read_bytes()).hexdigest(),
                        "old_evidence_tree_sha256": EVIDENCE,
                        "new_evidence_tree_sha256": REPLACEMENT_EVIDENCE,
                    }
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return manifest


def test_the_pointer_is_written_last_and_names_the_new_layout(tmp_path):
    client = MemoryR2()
    release = publish_snapshot(client, "bucket", build_site(tmp_path, "one", [_identifier(1)]))

    assert client.put_order[-1] == POINTER_KEY
    base_key = f"snapshots/{release}/{release_delta.BASE_PATH}"
    base = release_delta.parse_base(client.objects[base_key]["Body"])
    assert json.loads(client.objects[POINTER_KEY]["Body"]) == {
        "schema_version": 3,
        "release": release,
        "publication_base": release_delta.base_id(base),
    }
    assert f"public/entries/{_identifier(1)}.json" in client.objects
    assert f"snapshots/{release}/index.json" in client.objects
    assert f"snapshots/{release}/release-delta.json" in client.objects


def test_publishing_an_added_entry_writes_only_that_entry_and_the_aggregates(tmp_path):
    """The whole point. A second entry must not rewrite the first one.

    This is the test that would notice the quadratic behaviour returning.
    """
    client = MemoryR2()
    first, second = _identifier(1), _identifier(2)
    publish_snapshot(client, "bucket", build_site(tmp_path, "one", [first]))
    client.put_order.clear()
    release = publish_snapshot(client, "bucket", build_site(tmp_path, "two", [first, second]))

    written = set(client.put_order)
    assert not any(key.startswith(f"public/entries/{first}") for key in written)
    assert not any(key.startswith(f"public/renders/{first}") for key in written)
    assert not any(key.startswith(f"public/evidence/{first}") for key in written)
    assert f"public/entries/{second}.json" in written
    assert f"public/renders/{second}/{TREE}/Challenge/index.html" in written
    assert f"snapshots/{release}/index.json" in written
    # Three objects for the new record, and the pages this site stages, which
    # is all of them because `build_site` is a full rebuild. Which pages an
    # ordinary release stages is the stager's decision and is measured in
    # `test_growth.py`; what is settled here is that a record already published
    # is not rewritten, whatever else is.
    assert sorted(key for key in written if key.startswith("public/")) == sorted(
        [
            f"public/entries/{second}.json",
            f"public/renders/{second}/{TREE}/Challenge/index.html",
            f"public/evidence/{second}/{EVIDENCE}/review.json",
            "public/feed.xml",
            "public/feeds/msc/00A05.xml",
            "public/feeds/msc/01A05.xml",
            f"public/browse/{_day(second)}/{_page(second)}.json",
            f"public/browse/{_day(second)[:4]}.json",
            "public/browse/index.json",
        ]
    )


def test_the_browse_directory_is_written_after_every_shard_it_names(tmp_path):
    """A reader takes the directory as its account of which shards hold
    anything, and skips the ones it says are empty. Written first, it could
    tell a reader a shard was empty while the record that filled it was
    already being served, and the record would be missing from browsing with
    nothing failing to say so."""
    client = MemoryR2()
    publish_snapshot(client, "bucket", build_site(tmp_path, "one", [_identifier(1)]))
    site = build_site(tmp_path, "two", [_identifier(1), _identifier(2)])
    client.put_order.clear()
    publish_snapshot(client, "bucket", site)

    written = [key for key in client.put_order if key.startswith("public/browse/")]
    assert written[-1] == "public/browse/index.json", written


def test_subject_pages_are_written_before_the_documents_that_name_them(tmp_path):
    client = MemoryR2()
    paths = [
        "subjects/msc/11A05.json",
        "subjects/msc/11A05/2026.json",
        "subjects/msc/11A05/2026-08-07/1.json",
    ]
    site = build_site(tmp_path, "one", [_identifier(1)])
    stage_stable_documents(site, paths)
    publish_snapshot(client, "bucket", site)

    assert [key for key in client.put_order if key.startswith("public/subjects/")] == [
        "public/subjects/msc/11A05/2026-08-07/1.json",
        "public/subjects/msc/11A05/2026.json",
        "public/subjects/msc/11A05.json",
    ]


def test_a_record_keeps_its_key_across_releases(tmp_path):
    client = MemoryR2()
    first = _identifier(1)
    publish_snapshot(client, "bucket", build_site(tmp_path, "one", [first]))
    before = client.keys("public/")
    publish_snapshot(client, "bucket", build_site(tmp_path, "two", [first, _identifier(2)]))

    assert before <= client.keys("public/"), "a published record moved"


def test_only_the_current_and_previous_snapshots_are_retained(tmp_path):
    client = MemoryR2()
    entries = [_identifier(1)]
    one = publish_snapshot(client, "bucket", build_site(tmp_path, "one", entries))
    two = publish_snapshot(client, "bucket", build_site(tmp_path, "two", entries + [_identifier(2)]))
    three = publish_snapshot(
        client, "bucket", build_site(tmp_path, "three", entries + [_identifier(2), _identifier(3)])
    )

    kept = {key.split("/")[1] for key in client.keys("snapshots/")}
    assert kept == {two, three}
    assert one not in kept


def test_a_withdrawn_record_is_taken_out_of_the_public_prefix(tmp_path):
    client = MemoryR2()
    first, second = _identifier(1), _identifier(2)
    publish_snapshot(client, "bucket", build_site(tmp_path, "one", [first, second]))
    publish_snapshot(
        client, "bucket", build_site(tmp_path, "two", [first, second], taken_down=[second])
    )

    remaining = client.keys("public/")
    assert not any(
        key.startswith(("public/entries/", "public/renders/", "public/evidence/"))
        and second in key
        for key in remaining
    ), "a withdrawn record is still served"
    assert f"public/tombstones/{second}.json" in remaining
    assert any(first in key for key in remaining), "the wrong record was withdrawn"


def test_a_removal_without_a_tombstone_stops_the_publication(tmp_path):
    """A record leaves the staged set because it was taken down, or because the
    stager is broken. Only one of those may delete published bytes."""
    client = MemoryR2()
    first, second = _identifier(1), _identifier(2)
    publish_snapshot(client, "bucket", build_site(tmp_path, "one", [first, second]))
    pointer = bytes(client.objects[POINTER_KEY]["Body"])

    with pytest.raises(RuntimeError, match="no tombstone"):
        publish_snapshot(client, "bucket", build_site(tmp_path, "two", [first]))
    assert bytes(client.objects[POINTER_KEY]["Body"]) == pointer, "the pointer moved anyway"
    assert any(second in key for key in client.keys("public/"))


def test_a_delete_r2_refuses_stops_the_publication_before_the_pointer_moves(tmp_path):
    """A batch delete answers 200 with a per-object error array. Ignoring it
    would leave a withdrawn record served while the index said it was gone."""
    client = MemoryR2()
    first, second = _identifier(1), _identifier(2)
    publish_snapshot(client, "bucket", build_site(tmp_path, "one", [first, second]))
    pointer = bytes(client.objects[POINTER_KEY]["Body"])
    client.delete_errors = {f"public/entries/{second}.json": "denied"}

    with pytest.raises(RuntimeError, match="refused to delete"):
        publish_snapshot(
            client, "bucket", build_site(tmp_path, "two", [first, second], taken_down=[second])
        )
    assert bytes(client.objects[POINTER_KEY]["Body"]) == pointer, "the pointer moved anyway"


def test_a_record_whose_bytes_changed_stops_the_publication(tmp_path):
    client = MemoryR2()
    first = _identifier(1)
    publish_snapshot(client, "bucket", build_site(tmp_path, "one", [first]))
    altered = build_site(tmp_path, "two", [first])
    record = altered / "entries" / f"{first}.json"
    record.write_text(json.dumps({"id": first, "tampered": True}) + "\n")
    data = record.read_bytes()
    delta = release_delta.parse((altered / "release-delta.json").read_bytes())
    for row in delta["additions"]:
        if row["path"] == f"entries/{first}.json":
            row["bytes"] = len(data)
            row["sha256"] = hashlib.sha256(data).hexdigest()
    delta["records"]["root"] = release_delta.root_of(delta["additions"])
    (altered / "release-delta.json").write_bytes(release_delta.canonical_bytes(delta))

    # R2 refuses to overwrite the record, so what stops this is the readback:
    # the bytes at the key are not the bytes this release describes.
    with pytest.raises(RuntimeError, match="readback verification failed"):
        publish_snapshot(client, "bucket", altered)


def test_manifest_bound_replacement_rebinds_only_the_named_record(tmp_path):
    client = MemoryR2()
    first = _identifier(1)
    original = build_site(tmp_path, "one", [first])
    old_entry = (original / "entries" / f"{first}.json").read_bytes()
    publish_snapshot(client, "bucket", original)
    replacement = build_site(tmp_path, "two", [first])
    manifest = prepare_record_replacement(tmp_path, replacement, first, old_entry)
    client.put_order.clear()

    publish_snapshot(
        client, "bucket", replacement, record_replacements=manifest
    )

    entry_key = f"public/entries/{first}.json"
    new_evidence_key = (
        f"public/evidence/{first}/{REPLACEMENT_EVIDENCE}/review.json"
    )
    assert client.objects[entry_key]["Body"] == (
        replacement / "entries" / f"{first}.json"
    ).read_bytes()
    assert new_evidence_key in client.objects
    assert client.put_order.index(new_evidence_key) < client.put_order.index(entry_key)
    assert client.put_order.index(entry_key) < client.put_order.index(POINTER_KEY)
    assert not any(
        key.startswith(f"public/evidence/{first}/{EVIDENCE}/")
        for key in client.objects
    )


def test_record_replacement_refuses_origin_bytes_outside_the_manifest(tmp_path):
    client = MemoryR2()
    first = _identifier(1)
    original = build_site(tmp_path, "one", [first])
    old_entry = (original / "entries" / f"{first}.json").read_bytes()
    publish_snapshot(client, "bucket", original)
    replacement = build_site(tmp_path, "two", [first])
    manifest = prepare_record_replacement(tmp_path, replacement, first, old_entry)
    entry_key = f"public/entries/{first}.json"
    client.objects[entry_key]["Body"] = b"unmanifested drift\n"
    pointer = bytes(client.objects[POINTER_KEY]["Body"])

    with pytest.raises(RuntimeError, match="origin does not match"):
        publish_snapshot(
            client, "bucket", replacement, record_replacements=manifest
        )

    assert bytes(client.objects[POINTER_KEY]["Body"]) == pointer


def test_record_replacement_refuses_an_unbound_old_evidence_tree(tmp_path):
    client = MemoryR2()
    first = _identifier(1)
    original = build_site(tmp_path, "one", [first])
    old_entry = (original / "entries" / f"{first}.json").read_bytes()
    publish_snapshot(client, "bucket", original)
    replacement = build_site(tmp_path, "two", [first])
    manifest = prepare_record_replacement(tmp_path, replacement, first, old_entry)
    old_review_key = f"public/evidence/{first}/{EVIDENCE}/review.json"
    client.objects[old_review_key]["Body"] = b"unmanifested evidence drift\n"
    pointer = bytes(client.objects[POINTER_KEY]["Body"])

    with pytest.raises(RuntimeError, match="origin review does not match"):
        publish_snapshot(
            client, "bucket", replacement, record_replacements=manifest
        )

    assert bytes(client.objects[POINTER_KEY]["Body"]) == pointer


def test_record_replacement_is_retry_safe_after_the_pointer_moves(tmp_path):
    client = MemoryR2()
    first = _identifier(1)
    original = build_site(tmp_path, "one", [first])
    old_entry = (original / "entries" / f"{first}.json").read_bytes()
    publish_snapshot(client, "bucket", original)
    replacement = build_site(tmp_path, "two", [first])
    manifest = prepare_record_replacement(tmp_path, replacement, first, old_entry)

    release = publish_snapshot(
        client, "bucket", replacement, record_replacements=manifest
    )
    again = publish_snapshot(
        client, "bucket", replacement, record_replacements=manifest
    )

    assert again == release


def test_an_orphan_from_an_interrupted_publication_is_cleared(tmp_path):
    client = MemoryR2()
    first = _identifier(1)
    publish_snapshot(client, "bucket", build_site(tmp_path, "one", [first]))
    client.objects["public/leftover.json"] = {"Body": b"{}", "Metadata": {}}

    publish_snapshot(client, "bucket", build_site(tmp_path, "two", [first, _identifier(2)]))
    assert "public/leftover.json" not in client.objects


def test_a_retry_after_the_pointer_moved_keeps_the_release_it_would_roll_back_to(tmp_path):
    """The pointer cannot say what came before it once it has been moved, so a
    retry must not use it to decide what to delete."""
    client = MemoryR2()
    entries = [_identifier(1)]
    one = publish_snapshot(client, "bucket", build_site(tmp_path, "one", entries))
    site = build_site(tmp_path, "two", entries + [_identifier(2)])
    two = publish_snapshot(client, "bucket", site)
    assert one in {key.split("/")[1] for key in client.keys("snapshots/")}

    again = publish_snapshot(client, "bucket", site)
    assert again == two
    kept = {key.split("/")[1] for key in client.keys("snapshots/")}
    assert one in kept, "the retry deleted the release a rollback would need"


def test_availability_is_not_carried_in_a_release(tmp_path):
    """A six-hourly refresh of one file must not mint a release id for the
    whole dataset, which is what put it in the manifest in the first place."""
    client = MemoryR2()
    entries = [_identifier(1)]
    without = publish_snapshot(client, "bucket", build_site(tmp_path, "one", entries))
    with_it = publish_snapshot(
        client, "bucket", build_site(tmp_path, "two", entries, availability=True)
    )
    assert without == with_it, "staging availability changed the release id"
    assert not any("source-availability" in key for key in client.objects)


def test_a_manifest_that_names_availability_is_refused(tmp_path):
    client = MemoryR2()
    site = build_site(tmp_path, "one", [_identifier(1)], availability=True)
    delta = release_delta.parse((site / "release-delta.json").read_bytes())
    data = (site / "source-availability.json").read_bytes()
    delta["aggregates"].append(
        {
            "path": "source-availability.json",
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    )
    delta["aggregates"].sort(key=lambda row: row["path"])
    (site / "release-delta.json").write_bytes(release_delta.canonical_bytes(delta))

    with pytest.raises(ValueError, match="source-availability"):
        publish_snapshot(client, "bucket", site)


def test_a_listing_longer_than_one_page_is_followed(tmp_path):
    client = MemoryR2()
    client.page_size = 2
    entries = [_identifier(n) for n in range(1, 5)]
    publish_snapshot(client, "bucket", build_site(tmp_path, "one", entries))
    client.put_order.clear()
    publish_snapshot(client, "bucket", build_site(tmp_path, "two", entries))

    assert not [
        key for key in client.put_order
        if key.startswith(("public/entries/", "public/renders/", "public/evidence/"))
    ], "a paginated listing was truncated, so settled records were rewritten"


def test_audit_reports_a_record_that_stopped_being_there(tmp_path):
    client = MemoryR2()
    first = _identifier(1)
    publish_snapshot(client, "bucket", build_site(tmp_path, "one", [first]))
    assert audit(client, "bucket") == []

    client.objects.pop(f"public/entries/{first}.json")
    problems = audit(client, "bucket")
    # The root is over the whole set, so a record that stopped being there is
    # reported as the set no longer being the one the release describes. That
    # catches a substitution too, which comparing a list of names would not.
    assert len(problems) == 1
    assert "claims" in problems[0] and "root is" in problems[0]


def test_the_path_classes_agree_with_the_table_the_worker_is_read_against():
    fixture = json.loads(
        (pathlib.Path(__file__).resolve().parents[1] / "tests" / "path-classes.json").read_text()
    )
    assert {path: classify(path) for path in fixture["paths"]} == fixture["paths"]


def _database(root: pathlib.Path, launched: bool) -> pathlib.Path:
    database = root / "canonical"
    database.mkdir(parents=True, exist_ok=True)
    (database / "schema-v3.json").write_text("{}\n")
    (database / "scores-v1.json").write_text("{}\n")
    if launched:
        (database / ".palomar-launched").write_text("")
    return database


def test_before_launch_a_record_may_simply_stop_being_staged(tmp_path):
    """A pre-launch database is deliberately reshapeable: its records are not
    publication history yet, so dropping one is not a withdrawal."""
    client = MemoryR2()
    first, second = _identifier(1), _identifier(2)
    database = _database(tmp_path, launched=False)
    publish_snapshot(client, "bucket", build_site(tmp_path, "one", [first, second]), database)
    publish_snapshot(client, "bucket", build_site(tmp_path, "two", [first]), database)

    assert not any(second in key for key in client.keys("public/"))
    assert any(first in key for key in client.keys("public/"))


def test_after_launch_the_same_drop_stops_the_publication(tmp_path):
    client = MemoryR2()
    first, second = _identifier(1), _identifier(2)
    database = _database(tmp_path, launched=True)
    publish_snapshot(client, "bucket", build_site(tmp_path, "one", [first, second]), database)
    pointer = bytes(client.objects[POINTER_KEY]["Body"])

    with pytest.raises(RuntimeError, match="no tombstone"):
        publish_snapshot(client, "bucket", build_site(tmp_path, "two", [first]), database)
    assert bytes(client.objects[POINTER_KEY]["Body"]) == pointer


def test_pointing_it_at_the_wrong_directory_fails_rather_than_disarming(tmp_path):
    """Silently reading "not launched" from a directory that is not the
    database would turn the guard off exactly when it is needed."""
    client = MemoryR2()
    with pytest.raises(RuntimeError, match="does not look like the canonical database"):
        publish_snapshot(
            client, "bucket", build_site(tmp_path, "one", [_identifier(1)]), tmp_path / "nowhere"
        )


# An immutable object is created, never written over.


def test_a_published_record_is_not_rewritten_when_the_manifest_is_lost(tmp_path):
    """The bug this closes.

    An object that exists but that nothing attests used to be overwritten,
    on the reasoning that overwriting is cheaper than deciding whether to
    believe it. That made losing one manifest -- a half-finished retention
    sweep, a misread pointer -- into a silent rewrite of every published
    record, which is the one thing the append-only invariant forbids.
    """
    client = MemoryR2()
    identifier = _identifier(1)
    release = publish_snapshot(client, "bucket", build_site(tmp_path, "one", [identifier]))
    original = dict(client.objects[f"public/entries/{identifier}.json"])

    # The publisher now has no idea what it wrote last time.
    del client.objects[f"snapshots/{release}/release-delta.json"]
    client.put_order.clear()
    client.refused.clear()
    publish_snapshot(client, "bucket", build_site(tmp_path, "two", [identifier]))

    assert f"public/entries/{identifier}.json" in client.refused
    assert f"public/entries/{identifier}.json" not in client.put_order
    assert client.objects[f"public/entries/{identifier}.json"] == original


def test_a_published_record_whose_bytes_disagree_stops_the_publication(tmp_path):
    """Refusing the write is not enough on its own: something has to notice.

    R2 keeps whatever is there, so a publication that offers different bytes
    at a taken key would otherwise move the pointer over an origin serving
    something the release does not describe.
    """
    client = MemoryR2()
    identifier = _identifier(1)
    release = publish_snapshot(client, "bucket", build_site(tmp_path, "one", [identifier]))
    del client.objects[f"snapshots/{release}/release-delta.json"]
    client.objects[f"public/entries/{identifier}.json"]["Body"] = b'{"id": "tampered"}\n'

    with pytest.raises(RuntimeError, match="readback verification failed"):
        publish_snapshot(client, "bucket", build_site(tmp_path, "two", [identifier]))


def test_a_publication_that_crashed_after_writing_can_finish(tmp_path):
    """The state a create-only write has to tolerate.

    Objects are written before the pointer moves, so a crash in between
    leaves them present and unattested. The retry has to complete rather
    than fail on its own earlier work.
    """
    client = MemoryR2()
    identifier = _identifier(1)
    site = build_site(tmp_path, "one", [identifier])

    # Exactly what a crash before the flip leaves behind.
    for relative in (
        f"entries/{identifier}.json",
        f"renders/{identifier}/{TREE}/Challenge/index.html",
    ):
        data = (site / relative).read_bytes()
        client.objects[f"public/{relative}"] = {
            "Body": data,
            "Metadata": {"sha256": hashlib.sha256(data).hexdigest()},
        }

    release = publish_snapshot(client, "bucket", site)
    assert json.loads(client.objects[POINTER_KEY]["Body"])["release"] == release
    offered = [key for key in client.refused if key.startswith("public/")]
    assert len(offered) == 2, "it offered them and R2 kept what was there"


def test_aggregates_are_still_written_unconditionally(tmp_path):
    """They are release-keyed and change on every publication; a conditional
    write would make a retry of a flipped release fail on its own output."""
    client = MemoryR2()
    site = build_site(tmp_path, "one", [_identifier(1)])
    release = publish_snapshot(client, "bucket", site)
    client.refused.clear()
    assert publish_snapshot(client, "bucket", site) == release
    # The records are offered and refused, which is the point of create-only.
      # No aggregate or feed write may be conditional: those change every
      # release, so a conditional one would fail on a retry of its own output.
    assert [
        key
        for key in client.refused
        if not key.startswith(("internal/", "public/entries/", "public/renders/", "public/evidence/"))
    ] == []


def test_an_origin_that_ignores_conditional_writes_stops_the_publication(tmp_path):
    """Everything above rests on R2 refusing a write to a taken key.

    An origin that ignored the header would accept every conditional write,
    the publisher would carry on, and the guarantee would simply be untrue
    with nothing failing to say so. Two operations a publication buy the
    difference between a checked guarantee and a believed one.
    """

    class Permissive(MemoryR2):
        def put_object(self, *, IfNoneMatch=None, **arguments):
            return super().put_object(**arguments)

    client = Permissive()
    with pytest.raises(RuntimeError, match="not protected from being overwritten"):
        publish_snapshot(client, "bucket", build_site(tmp_path, "one", [_identifier(1)]))
    assert POINTER_KEY not in client.objects, "it stopped before publishing anything"


def test_the_probe_is_not_served_and_not_audited(tmp_path):
    client = MemoryR2()
    publish_snapshot(client, "bucket", build_site(tmp_path, "one", [_identifier(1)]))
    assert client.keys("internal/"), "the probe ran"
    assert not client.keys("public/internal")
    assert audit(client, "bucket") == [], "the probe is not part of the dataset"


def test_the_publisher_writes_the_pages_the_release_staged_and_no_others(tmp_path):
    """Where the decision about which pages move now lives.

    The publisher used to compare every staged page against the parent's row
    for it and skip the ones that matched, because a release staged every page
    in the dataset and a handful had moved. The parent's delta names only what
    its release wrote now, so there is nothing to compare against and nothing
    to skip: a release stages the pages it changed, and the publisher writes
    exactly those. The growth that arrangement exists for is measured where it
    is decided, in `test_growth.py`.
    """
    client = MemoryR2()
    first, second = _identifier(1), _identifier(2)
    publish_snapshot(client, "bucket", build_site(tmp_path, "one", [first], categories=400))
    client.put_order.clear()
    site = build_site(tmp_path, "two", [first, second], categories=400)
    publish_snapshot(client, "bucket", site)

    delta = release_delta.parse((site / "release-delta.json").read_bytes())
    stable = {f"public/{row['path']}" for row in delta["stable"]}
    written = {
        key for key in client.put_order
        if key.startswith("public/")
        and not key.startswith(("public/entries/", "public/renders/", "public/evidence/"))
    }
    assert written == stable


def test_the_publisher_leaves_public_cache_policy_to_the_worker(tmp_path):
    client = MemoryR2()
    publish_snapshot(client, "bucket", build_site(tmp_path, "one", [_identifier(1)]))

    public_objects = {
        key: value for key, value in client.objects.items() if key.startswith("public/")
    }
    assert public_objects
    assert all("CacheControl" not in value for value in public_objects.values())


def test_a_feed_is_rewritten_when_its_bytes_change(tmp_path):
    """Skipping unchanged feeds must not skip changed ones: a takedown has to
    reach every feed that mentioned the record."""
    client = MemoryR2()
    first, second = _identifier(1), _identifier(2)
    publish_snapshot(client, "bucket", build_site(tmp_path, "one", [first, second]))
    before = client.objects["public/feed.xml"]["Body"]
    client.put_order.clear()

    publish_snapshot(
        client, "bucket", build_site(tmp_path, "two", [first, second], taken_down=[second])
    )

    assert "public/feed.xml" in client.put_order
    assert client.objects["public/feed.xml"]["Body"] != before
    assert f"public/entries/{second}.json" not in client.objects, "the record went too"


def test_a_retired_feed_is_withdrawn_without_a_tombstone(tmp_path):
    """A feed is derived, not promised. It needs no takedown to disappear, and
    requiring one would mean a category could never stop being served."""
    client = MemoryR2()
    identifier = _identifier(1)
    publish_snapshot(client, "bucket", build_site(tmp_path, "one", [identifier], categories=3))
    assert "public/feeds/msc/02A05.xml" in client.objects

    publish_snapshot(client, "bucket", build_site(tmp_path, "two", [identifier], categories=2))
    assert "public/feeds/msc/02A05.xml" not in client.objects
    assert f"public/entries/{identifier}.json" in client.objects


def test_the_availability_manifest_is_never_withdrawn_by_this_publisher(tmp_path):
    """It sits at a stable key like a feed, but another tool owns it. Treating
    "not staged here" as "no longer wanted" would delete it every publication."""
    client = MemoryR2()
    client.objects["public/source-availability.json"] = {
        "Body": b"{}\n", "Metadata": {"sha256": hashlib.sha256(b"{}\n").hexdigest()}
    }
    publish_snapshot(client, "bucket", build_site(tmp_path, "one", [_identifier(1)]))
    assert "public/source-availability.json" in client.objects


# An incremental release: what the publisher does when the delta names a parent.


def _incremental(base_delta, site, *, entries, added, withdrawn=(), count=None):
    """Turn a fixture site's full delta into one that claims a parent."""
    delta = release_delta.parse((site / "release-delta.json").read_bytes())
    keep = [
        row for row in delta["additions"]
        if any(name in row["path"] for name in added)
    ]
    removed = [
        row for row in base_delta["additions"]
        if any(name in row["path"] for name in withdrawn)
    ]
    delta["parent"] = release_delta.release_id(base_delta)
    delta["additions"] = keep
    delta["withdrawals"] = sorted(removed, key=lambda row: row["path"])
    delta["takedowns_git_blob"] = base_delta["takedowns_git_blob"]
    delta["records"] = {
        "count": len(entries),
        "root": release_delta.root_after(
            base_delta["records"]["root"], keep, delta["withdrawals"]
        ),
    }
    (site / "release-delta.json").write_bytes(release_delta.canonical_bytes(delta))
    return delta


def test_an_incremental_release_never_lists_the_bucket(tmp_path):
    """The listing is O(S) per publication, which is the last of the three
    whole-dataset walks a publication used to do."""
    client = MemoryR2()
    first, second = _identifier(1), _identifier(2)
    publish_snapshot(client, "bucket", build_site(tmp_path, "one", [first]))
    base = release_delta.parse(
        client.objects[
            f"snapshots/{json.loads(client.objects[POINTER_KEY]['Body'])['release']}/release-delta.json"
        ]["Body"]
    )

    site = build_site(tmp_path, "two", [first, second])
    _incremental(base, site, entries=[first, second], added=[second])
    client.list_calls = []
    original = client.list_objects_v2

    def counted(**arguments):
        client.list_calls.append(arguments["Prefix"])
        return original(**arguments)

    client.list_objects_v2 = counted
    publish_snapshot(client, "bucket", site)

    assert not any(prefix == "public/" for prefix in client.list_calls), client.list_calls


def test_accepted_publication_work_is_constant_after_many_takedowns(tmp_path):
    """Historical withdrawals live behind stable keys and a constant parent base.

    The accepted event neither downloads the parent's full O(W) delta nor
    rewrites W tombstones.  Count calls and bytes, not just Python containers,
    so a future regression that reintroduces either term is visible.
    """
    observed = {}
    for withdrawn_count in (1, 100):
        root = tmp_path / str(withdrawn_count)
        client = MemoryR2()
        active = _identifier(1)
        withdrawn = [_identifier(serial) for serial in range(2, withdrawn_count + 2)]
        all_existing = [active, *withdrawn]
        release = publish_snapshot(
            client,
            "bucket",
            build_site(root, "base", all_existing, taken_down=withdrawn),
        )
        parent_delta_key = f"snapshots/{release}/release-delta.json"
        base = release_delta.parse(client.objects[parent_delta_key]["Body"])
        # The next accepted event is intentionally unable to read the full
        # parent delta. Its authenticated publication-base projection is enough.
        del client.objects[parent_delta_key]

        newcomer = _identifier(withdrawn_count + 2)
        site = build_site(
            root,
            "accepted",
            [*all_existing, newcomer],
            taken_down=withdrawn,
        )
        delta = _incremental(
            base,
            site,
            entries=[active, newcomer],
            added=[newcomer],
        )
        delta["stable"] = [
            row for row in delta["stable"]
            if not row["path"].startswith("tombstones/")
        ]
        for tombstone in (site / "tombstones").glob("*.json"):
            tombstone.unlink()
        (site / "release-delta.json").write_bytes(release_delta.canonical_bytes(delta))

        counts = {"head": 0, "get": 0, "put": 0, "list": 0}
        for name, key in (
            ("head_object", "head"),
            ("get_object", "get"),
            ("put_object", "put"),
            ("list_objects_v2", "list"),
        ):
            original = getattr(client, name)

            def counted(*args, _original=original, _key=key, **kwargs):
                counts[_key] += 1
                return _original(*args, **kwargs)

            setattr(client, name, counted)
        client.put_order.clear()
        publish_snapshot(client, "bucket", site)
        written_bytes = sum(len(client.objects[key]["Body"]) for key in client.put_order)
        observed[withdrawn_count] = counts, written_bytes, len(
            release_delta.canonical_bytes(delta)
        )

        assert not any(
            row["path"].startswith("tombstones/") for row in delta["stable"]
        )

    assert observed[1] == observed[100], observed


def test_an_incremental_release_writes_only_what_it_declares(tmp_path):
    client = MemoryR2()
    first, second = _identifier(1), _identifier(2)
    publish_snapshot(client, "bucket", build_site(tmp_path, "one", [first]))
    base = release_delta.parse(
        client.objects[
            f"snapshots/{json.loads(client.objects[POINTER_KEY]['Body'])['release']}/release-delta.json"
        ]["Body"]
    )
    client.put_order.clear()

    site = build_site(tmp_path, "two", [first, second])
    _incremental(base, site, entries=[first, second], added=[second])
    publish_snapshot(client, "bucket", site)

    records = [
        key for key in client.put_order
        if key.startswith(("public/entries/", "public/renders/", "public/evidence/"))
    ]
    assert records and all(second in key for key in records), records


def test_a_release_whose_arithmetic_does_not_hold_is_refused(tmp_path):
    """The membership invariant, proved from two small documents instead of a
    listing. A stager that dropped a record has to declare a withdrawal to keep
    the count, and the right one to keep the root."""
    client = MemoryR2()
    first, second = _identifier(1), _identifier(2)
    publish_snapshot(client, "bucket", build_site(tmp_path, "one", [first]))
    base = release_delta.parse(
        client.objects[
            f"snapshots/{json.loads(client.objects[POINTER_KEY]['Body'])['release']}/release-delta.json"
        ]["Body"]
    )

    site = build_site(tmp_path, "two", [first, second])
    delta = _incremental(base, site, entries=[first, second], added=[second])
    delta["records"]["count"] += 1
    (site / "release-delta.json").write_bytes(release_delta.canonical_bytes(delta))
    with pytest.raises(RuntimeError, match="active records"):
        publish_snapshot(client, "bucket", site)

    delta["records"]["count"] -= 1
    delta["records"]["root"] = "f" * 64
    (site / "release-delta.json").write_bytes(release_delta.canonical_bytes(delta))
    with pytest.raises(RuntimeError, match="not its parent's root"):
        publish_snapshot(client, "bucket", site)


def test_a_publication_base_that_disagrees_with_the_pointer_is_not_believed(tmp_path):
    client = MemoryR2()
    first, second = _identifier(1), _identifier(2)
    release = publish_snapshot(client, "bucket", build_site(tmp_path, "one", [first]))
    parent = release_delta.parse(
        client.objects[f"snapshots/{release}/release-delta.json"]["Body"]
    )
    key = f"snapshots/{release}/{release_delta.BASE_PATH}"
    base = release_delta.parse_base(client.objects[key]["Body"])
    base["database_commit"] = "1" * 40
    client.objects[key]["Body"] = release_delta.canonical_base_bytes(base)
    site = build_site(tmp_path, "two", [first, second])
    _incremental(parent, site, entries=[first, second], added=[second])

    with pytest.raises(RuntimeError, match="does not match the R2 pointer"):
        publish_snapshot(client, "bucket", site)


@pytest.mark.parametrize("corruption", ["digest", "malformed", "noncanonical"])
def test_a_full_release_recovers_from_a_corrupt_current_publication_base(
    tmp_path, corruption
):
    """Recovery states the complete set and must not depend on broken state."""
    client = MemoryR2()
    first, second = _identifier(1), _identifier(2)
    old = publish_snapshot(client, "bucket", build_site(tmp_path, "one", [first]))
    key = f"snapshots/{old}/{release_delta.BASE_PATH}"
    pointer = json.loads(client.objects[POINTER_KEY]["Body"])
    if corruption == "digest":
        client.objects[key]["Body"] = b"{}\n"
    elif corruption == "malformed":
        client.objects[key]["Body"] = b"{\n"
        pointer["publication_base"] = hashlib.sha256(b"{\n").hexdigest()
        client.objects[POINTER_KEY]["Body"] = json.dumps(pointer).encode()
    else:
        client.objects[key]["Body"] += b"\n"
        pointer["publication_base"] = hashlib.sha256(
            client.objects[key]["Body"]
        ).hexdigest()
        client.objects[POINTER_KEY]["Body"] = json.dumps(pointer).encode()

    current = publish_snapshot(
        client, "bucket", build_site(tmp_path, "two", [first, second])
    )

    assert current != old
    assert json.loads(client.objects[POINTER_KEY]["Body"])["release"] == current
    assert f"public/entries/{first}.json" in client.objects
    assert f"public/entries/{second}.json" in client.objects


def test_reading_a_corrupt_current_base_requests_a_full_rebuild(
    tmp_path, monkeypatch, capsys
):
    """A bad planning authority cannot select a scope or disable recovery."""
    client = MemoryR2()
    release = publish_snapshot(
        client, "bucket", build_site(tmp_path, "one", [_identifier(1)])
    )
    client.objects[f"snapshots/{release}/{release_delta.BASE_PATH}"]["Body"] = b"{}\n"
    target = tmp_path / "previous.json"

    import publish_snapshot as publisher

    monkeypatch.setattr(
        "sys.argv", ["publish_snapshot.py", "--write-current-base", str(target)]
    )
    for name in ("CLOUDFLARE_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "x")

    class Boto:
        def client(self, *_a, **_k):
            return client

    monkeypatch.setitem(__import__("sys").modules, "boto3", Boto())
    assert publisher.main() == 0
    assert target.read_bytes() == b""
    assert "served publication base is unusable" in capsys.readouterr().out


def test_an_incremental_release_whose_parent_cannot_be_read_is_refused(tmp_path):
    """It cannot check its own arithmetic, and guessing what the previous
    release contained is how a publication silently loses records."""
    client = MemoryR2()
    first, second = _identifier(1), _identifier(2)
    release = publish_snapshot(client, "bucket", build_site(tmp_path, "one", [first]))
    base = release_delta.parse(client.objects[f"snapshots/{release}/release-delta.json"]["Body"])
    site = build_site(tmp_path, "two", [first, second])
    _incremental(base, site, entries=[first, second], added=[second])
    del client.objects[f"snapshots/{release}/{release_delta.BASE_PATH}"]

    with pytest.raises(RuntimeError, match="could not be read"):
        publish_snapshot(client, "bucket", site)


def test_an_incremental_release_cannot_smuggle_a_withdrawal_or_tombstone(tmp_path):
    client = MemoryR2()
    first, second = _identifier(1), _identifier(2)
    release = publish_snapshot(client, "bucket", build_site(tmp_path, "one", [first, second]))
    base = release_delta.parse(client.objects[f"snapshots/{release}/release-delta.json"]["Body"])

    # Even a self-consistent hostile delta with a tombstone must take the full,
    # listing-backed path under the current contract.
    site = build_site(tmp_path, "two", [first, second], taken_down=[second])
    _incremental(base, site, entries=[first], added=[], withdrawn=[second])
    with pytest.raises(RuntimeError, match="may not withdraw records"):
        publish_snapshot(client, "bucket", site)
    assert f"public/entries/{second}.json" in client.objects
    assert f"public/entries/{first}.json" in client.objects

    # Removing the tombstone does not make the record withdrawal acceptable.
    client2 = MemoryR2()
    release = publish_snapshot(client2, "bucket", build_site(tmp_path, "three", [first, second]))
    base = release_delta.parse(client2.objects[f"snapshots/{release}/release-delta.json"]["Body"])
    bad = build_site(tmp_path, "four", [first, second])
    _incremental(base, bad, entries=[first], added=[], withdrawn=[second])
    with pytest.raises(RuntimeError, match="may not withdraw records"):
        publish_snapshot(client2, "bucket", bad)


def test_an_incremental_release_cannot_retire_a_stable_tombstone(tmp_path):
    client = MemoryR2()
    first, withdrawn = _identifier(1), _identifier(2)
    release = publish_snapshot(
        client,
        "bucket",
        build_site(tmp_path, "one", [first, withdrawn], taken_down=[withdrawn]),
    )
    base = release_delta.parse(
        client.objects[f"snapshots/{release}/release-delta.json"]["Body"]
    )
    tombstone = f"tombstones/{withdrawn}.json"
    site = build_site(
        tmp_path, "two", [first, withdrawn], taken_down=[withdrawn]
    )
    delta = _incremental(base, site, entries=[first], added=[])
    delta["stable"] = [row for row in delta["stable"] if row["path"] != tombstone]
    delta["retired"] = [tombstone]
    (site / "release-delta.json").write_bytes(release_delta.canonical_bytes(delta))

    with pytest.raises(RuntimeError, match="may not retire tombstones"):
        publish_snapshot(client, "bucket", site)
    assert f"public/{tombstone}" in client.objects


def test_a_declared_retirement_is_performed_without_a_tombstone(tmp_path):
    """A derived object needs no takedown to go, and could not have one: it
    belongs to no record. A postings head is the case that made this necessary,
    because a head left behind saying it has no results says that some withdrawn
    record carried that word."""
    client = MemoryR2()
    first = _identifier(1)
    release = publish_snapshot(client, "bucket", build_site(tmp_path, "one", [first]))
    base = release_delta.parse(client.objects[f"snapshots/{release}/release-delta.json"]["Body"])
    client.objects["public/search/t/counterexample/head.json"] = {"Body": b"{}\n"}

    site = build_site(tmp_path, "two", [first])
    delta = _incremental(base, site, entries=[first], added=[])
    delta["retired"] = ["search/t/counterexample/head.json"]
    (site / "release-delta.json").write_bytes(release_delta.canonical_bytes(delta))
    publish_snapshot(client, "bucket", site)

    assert "public/search/t/counterexample/head.json" not in client.objects
    assert f"public/entries/{first}.json" in client.objects


def test_a_head_is_deleted_before_the_pages_it_names(tmp_path):
    """The reverse of the dependency ranks used for writes. A page deleted first
    leaves a reader following a head that is still there to a 404; the head
    first leaves a page nothing points at, which no reader can reach."""
    client = MemoryR2()
    first = _identifier(1)
    release = publish_snapshot(client, "bucket", build_site(tmp_path, "one", [first]))
    base = release_delta.parse(client.objects[f"snapshots/{release}/release-delta.json"]["Body"])
    retired = ["search/t/ring/head.json", "search/t/ring/0.json", "search/t/ring/1.json"]
    for path in retired:
        client.objects[f"public/{path}"] = {"Body": b"{}\n"}

    site = build_site(tmp_path, "two", [first])
    delta = _incremental(base, site, entries=[first], added=[])
    delta["retired"] = sorted(retired)
    (site / "release-delta.json").write_bytes(release_delta.canonical_bytes(delta))
    publish_snapshot(client, "bucket", site)

    assert client.delete_order == [
        "public/search/t/ring/head.json",
        "public/search/t/ring/0.json",
        "public/search/t/ring/1.json",
    ]
    assert client.delete_calls == [
        ["public/search/t/ring/head.json"],
        ["public/search/t/ring/0.json", "public/search/t/ring/1.json"],
    ]


def test_a_subject_head_is_deleted_before_its_year_and_pages(tmp_path):
    """Deletion receives object keys, not the relative paths writes receive."""
    client = MemoryR2()
    first = _identifier(1)
    retired = [
        "subjects/msc/11A05.json",
        "subjects/msc/11A05/2026.json",
        "subjects/msc/11A05/2026-08-07/1.json",
    ]
    base_site = build_site(tmp_path, "one", [first])
    stage_stable_documents(base_site, retired)
    release = publish_snapshot(client, "bucket", base_site)
    base = release_delta.parse(
        client.objects[f"snapshots/{release}/release-delta.json"]["Body"]
    )

    site = build_site(tmp_path, "two", [first])
    delta = _incremental(base, site, entries=[first], added=[])
    delta["retired"] = sorted(retired)
    (site / "release-delta.json").write_bytes(release_delta.canonical_bytes(delta))
    publish_snapshot(client, "bucket", site)

    assert client.delete_order == [
        "public/subjects/msc/11A05.json",
        "public/subjects/msc/11A05/2026.json",
        "public/subjects/msc/11A05/2026-08-07/1.json",
    ]
    assert client.delete_calls == [
        ["public/subjects/msc/11A05.json"],
        ["public/subjects/msc/11A05/2026.json"],
        ["public/subjects/msc/11A05/2026-08-07/1.json"],
    ]
    assert not client.keys("public/subjects/msc/11A05")


def test_a_refused_subject_head_preserves_every_lower_rank(tmp_path):
    """A returned per-object error must stop before a year or page delete call."""
    client = MemoryR2()
    first = _identifier(1)
    retired = [
        "subjects/msc/11A05.json",
        "subjects/msc/11A05/2026.json",
        "subjects/msc/11A05/2026-08-07/1.json",
    ]
    base_site = build_site(tmp_path, "one", [first])
    stage_stable_documents(base_site, retired)
    release = publish_snapshot(client, "bucket", base_site)
    base = release_delta.parse(
        client.objects[f"snapshots/{release}/release-delta.json"]["Body"]
    )
    pointer = bytes(client.objects[POINTER_KEY]["Body"])
    head = "public/subjects/msc/11A05.json"
    client.delete_errors[head] = "denied"
    client.delete_calls.clear()

    site = build_site(tmp_path, "two", [first])
    delta = _incremental(base, site, entries=[first], added=[])
    delta["retired"] = sorted(retired)
    (site / "release-delta.json").write_bytes(release_delta.canonical_bytes(delta))

    with pytest.raises(RuntimeError, match="refused to delete"):
        publish_snapshot(client, "bucket", site)

    assert client.delete_calls == [[head]]
    assert all(f"public/{relative}" in client.objects for relative in retired)
    assert bytes(client.objects[POINTER_KEY]["Body"]) == pointer


def test_a_full_rebuild_removes_stale_subjects_in_dependency_order(tmp_path):
    """A rebuild infers stale stable keys from R2 rather than `retired`."""
    client = MemoryR2()
    first = _identifier(1)
    stale = [
        "subjects/msc/11A05.json",
        "subjects/msc/11A05/2026.json",
        "subjects/msc/11A05/2026-08-07/1.json",
    ]
    base_site = build_site(tmp_path, "one", [first])
    stage_stable_documents(base_site, stale)
    publish_snapshot(client, "bucket", base_site)
    client.delete_calls.clear()

    publish_snapshot(client, "bucket", build_site(tmp_path, "two", [first]))

    assert client.delete_calls == [
        ["public/subjects/msc/11A05.json"],
        ["public/subjects/msc/11A05/2026.json"],
        ["public/subjects/msc/11A05/2026-08-07/1.json"],
    ]
    assert not client.keys("public/subjects/msc/11A05")


def test_one_dependency_rank_is_batched_at_the_r2_limit():
    client = MemoryR2()
    keys = [f"public/browse/2026-08-07/{page}.json" for page in range(1, 1002)]
    for key in keys:
        client.objects[key] = {"Body": b"{}\n"}

    _delete_keys(client, "bucket", keys)

    assert [len(batch) for batch in client.delete_calls] == [1000, 1]
    assert not client.keys("public/browse/2026-08-07/")


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        ("browse/index.json", 2),
        ("browse/2026.json", 1),
        ("browse/2026-08-07/1.json", 0),
        ("search/t/ring/head.json", 2),
        ("search/t/ring/2026.json", 0),
        ("subjects/msc/11A05.json", 2),
        ("subjects/msc/11A05/2026.json", 1),
        ("subjects/msc/11A05/2026-08-07/1.json", 0),
        ("snapshots/" + "a" * 64 + "/browse/index.json", 0),
    ],
)
def test_relative_and_public_object_paths_have_the_same_rank(relative, expected):
    assert _rank(relative) == expected
    assert _rank(f"public/{relative}") == expected


def test_every_path_class_has_the_same_staged_and_public_rank():
    fixture = json.loads(
        (pathlib.Path(__file__).resolve().parents[1] / "tests" / "path-classes.json").read_text()
    )
    for public_path in fixture["paths"]:
        relative = public_path.removeprefix("/")
        assert _rank(relative) == _rank(f"public/{relative}"), public_path

    # The table deliberately includes snapshot and non-served shapes. They do
    # not use a public stable key, but exercising them proves prefix handling
    # cannot accidentally turn an unfamiliar shape into a new dependency.
    assert fixture["paths"]["/release-delta.json"] == "snapshot"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("subjects/msc/11A05.json", "subjects/msc/11A05.json"),
        ("public/subjects/msc/11A05.json", "subjects/msc/11A05.json"),
        ("publicity/subjects/msc/11A05.json", "publicity/subjects/msc/11A05.json"),
        ("/public/subjects/msc/11A05.json", "/public/subjects/msc/11A05.json"),
        ("public//subjects/msc/11A05.json", "/subjects/msc/11A05.json"),
        ("public/public/subjects/msc/11A05.json", "public/subjects/msc/11A05.json"),
        ("public/", ""),
        ("", ""),
    ],
)
def test_ordering_normalizes_only_the_exact_public_prefix(path, expected):
    assert _ordering_path(path) == expected


def test_a_retirement_of_something_this_publisher_does_not_own_is_refused(tmp_path):
    """The retirement list is the one place a stager can name a key for
    deletion without a tombstone to justify it, so it may only ever name a
    stable path in this layout: not a record, not one of the documents every
    release writes whatever the registry holds, and not an object this same
    release stages, which is written before the deletes run and would be
    removed after it."""
    first = _identifier(1)
    for number, path in enumerate((
        f"entries/{first}.json",
        "source-availability.json",
        "LICENSE",
        "search/t/RING/head.json",
        "recent.json",
        "browse/index.json",
    )):
        client = MemoryR2()
        release = publish_snapshot(
            client, "bucket", build_site(tmp_path, f"base{number}", [first])
        )
        base = release_delta.parse(
            client.objects[f"snapshots/{release}/release-delta.json"]["Body"]
        )
        site = build_site(tmp_path, f"next{number}", [first])
        delta = _incremental(base, site, entries=[first], added=[])
        delta["retired"] = [path]
        (site / "release-delta.json").write_bytes(release_delta.canonical_bytes(delta))
        with pytest.raises(RuntimeError, match="no tombstone"):
            publish_snapshot(client, "bucket", site)


def test_a_publication_holds_one_object_at_a_time(tmp_path, monkeypatch):
    """Memory must not grow with the release.

    Every staged byte used to be read up front and kept until a separate
    readback pass, which put a 7 GB runner within reach of a dataset it could
    otherwise publish. The hashing is the same work either way; it was the
    lifetime that was wrong. What that means operationally is that a read is
    always followed by the write and readback of the same object, never by
    another read -- so no object outlives its own round trip.
    """
    import publish_snapshot as publisher

    events: list[str] = []
    read = publisher._staged_bytes
    verify = publisher._verify

    monkeypatch.setattr(
        publisher, "_staged_bytes",
        lambda site, relative, digest: (events.append("read"), read(site, relative, digest))[1],
    )
    monkeypatch.setattr(
        publisher, "_verify",
        lambda *a, **k: (events.append("verify"), verify(*a, **k))[1],
    )
    client = MemoryR2()
    publish_snapshot(
        client, "bucket", build_site(tmp_path, "one", [_identifier(n) for n in range(1, 6)])
    )

    assert events.count("read") > 5, "nothing was read"
    assert "read" not in [
        events[index + 1]
        for index, event in enumerate(events[:-1])
        if event == "read"
    ], "two objects were read before either was written"


# The command line, which nothing exercised until it broke a publication.


@pytest.mark.parametrize(
    "arguments",
    [
        ["--audit"],
        ["--write-current-base", "somewhere.json"],
        ["--site", "somewhere"],
    ],
)
def test_every_mode_is_accepted_without_the_arguments_it_does_not_need(
    arguments, monkeypatch, tmp_path
):
    """A publication failed on this. `--write-current-base` reads the origin
    and writes a file; it has no site, and the argument check still demanded
    one, so the run stopped before it began. Nothing exercised `main` at all.
    """
    import publish_snapshot as publisher

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.argv", ["publish_snapshot.py", *arguments]
    )
    for name in ("CLOUDFLARE_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "x")
    monkeypatch.setattr(publisher, "audit", lambda *a, **k: [])
    monkeypatch.setattr(publisher, "publish_snapshot", lambda *a, **k: "0" * 64)
    monkeypatch.setattr(publisher, "_read_pointer", lambda *a, **k: None)

    class Boto:
        def client(self, *_a, **_k):
            return object()

    monkeypatch.setitem(__import__("sys").modules, "boto3", Boto())
    assert publisher.main() == 0


def test_reading_the_current_release_writes_something_staging_can_use(tmp_path, monkeypatch):
    client = MemoryR2()
    release = publish_snapshot(client, "bucket", build_site(tmp_path, "one", [_identifier(1)]))
    target = tmp_path / "previous.json"

    import publish_snapshot as publisher

    monkeypatch.setattr("sys.argv", [
        "publish_snapshot.py", "--write-current-base", str(target)
    ])
    for name in ("CLOUDFLARE_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "x")

    class Boto:
        def client(self, *_a, **_k):
            return client

    monkeypatch.setitem(__import__("sys").modules, "boto3", Boto())
    assert publisher.main() == 0
    assert release_delta.parse_base(target.read_bytes())["release"] == release


def test_nothing_published_yet_writes_an_empty_file(tmp_path, monkeypatch):
    """Which staging reads as "no parent", which is a full rebuild."""
    import publish_snapshot as publisher

    target = tmp_path / "previous.json"
    monkeypatch.setattr("sys.argv", [
        "publish_snapshot.py", "--write-current-base", str(target)
    ])
    for name in ("CLOUDFLARE_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "x")

    class Boto:
        def client(self, *_a, **_k):
            return MemoryR2()

    monkeypatch.setitem(__import__("sys").modules, "boto3", Boto())
    assert publisher.main() == 0
    assert target.read_bytes() == b""


# The whole-set check, moved out of the release and into the weekly sweep.


def test_reconciling_finds_a_page_that_stopped_being_there(tmp_path):
    """The check a release stopped being able to make.

    A release described itself with a row per stable object in the dataset, so
    it could tell a page that had drifted from one that had not -- and paid
    O(S) for that once per accepted result. The question is the same; what
    changed is that it is asked weekly, against a tree staged from the records
    rather than against a copy of the last answer.
    """
    client = MemoryR2()
    site = build_site(tmp_path, "one", [_identifier(1), _identifier(2)])
    publish_snapshot(client, "bucket", site)
    assert reconcile(client, "bucket", site) == []

    client.objects.pop("public/browse/index.json")
    assert reconcile(client, "bucket", site) == ["missing: public/browse/index.json"]


def test_reconciling_finds_a_page_whose_bytes_are_wrong(tmp_path):
    """Comparing names would not: a page that is there and says the wrong
    thing is exactly the failure a patched page can have."""
    client = MemoryR2()
    site = build_site(tmp_path, "one", [_identifier(1)])
    publish_snapshot(client, "bucket", site)

    client.objects["public/feed.xml"]["Body"] = b"<rss>something else</rss>\n"
    assert reconcile(client, "bucket", site) == ["wrong size: public/feed.xml"]


def test_reconciling_finds_a_page_nothing_should_have_written(tmp_path):
    client = MemoryR2()
    site = build_site(tmp_path, "one", [_identifier(1)])
    publish_snapshot(client, "bucket", site)

    # The postings head is the one that has to be here rather than assumed. It
    # is the only stable object a release deletes by declaring it, so nothing
    # lists the bucket to find out whether the delete happened; this is what
    # does, and it has to call a head a rebuild would not write unexpected
    # rather than treat its absence as the drift.
    for key in ("public/browse/1999-01-01/1.json", "public/search/t/counterexample/head.json"):
        client.put_object(
            Bucket="bucket", Key=key, Body=b"{}\n",
            ContentLength=3, ContentType="application/json",
            Metadata={"sha256": "0" * 64},
        )
    assert reconcile(client, "bucket", site) == [
        "unexpected: public/browse/1999-01-01/1.json",
        "unexpected: public/search/t/counterexample/head.json",
    ]


def test_reconciling_changes_nothing(tmp_path):
    """It runs on a schedule against a live bucket, and a sweep that could
    write is a sweep that can make things worse than it found them."""
    client = MemoryR2()
    site = build_site(tmp_path, "one", [_identifier(1)])
    publish_snapshot(client, "bucket", site)
    client.objects.pop("public/feed.xml")
    before = dict(client.objects)

    reconcile(client, "bucket", site)

    assert client.objects == before


def test_the_audit_still_refuses_a_key_nothing_could_serve(tmp_path):
    """`--audit` can no longer ask whether the current delta names an object,
    because a delta names only what its release wrote. So it asks the question
    that survives: is the origin holding something no URL could reach."""
    client = MemoryR2()
    publish_snapshot(client, "bucket", build_site(tmp_path, "one", [_identifier(1)]))
    assert audit(client, "bucket") == []

    client.put_object(
        Bucket="bucket", Key="public/browse/nonsense.json", Body=b"{}\n",
        ContentLength=3, ContentType="application/json",
        Metadata={"sha256": "0" * 64},
    )
    assert audit(client, "bucket") == ["unexpected: public/browse/nonsense.json"]


def test_the_audit_accepts_stable_tombstones_that_the_worker_serves(tmp_path):
    client = MemoryR2()
    first, withdrawn = _identifier(1), _identifier(2)
    publish_snapshot(
        client,
        "bucket",
        build_site(tmp_path, "withdrawn", [first, withdrawn], taken_down=[withdrawn]),
    )

    assert f"public/tombstones/{withdrawn}.json" in client.objects
    assert audit(client, "bucket") == []


def test_a_page_is_written_before_the_documents_that_name_it():
    """A reader takes a head as its account of which years are worth fetching
    and a year document as its account of which days and pages are, so
    everything a document names has to be at least as new as the document.

    Sorting the paths would nearly do it and gets one case wrong: a code's
    front page is `subjects/msc/11A05.json` and its pages are under
    `subjects/msc/11A05/`, and `.` sorts before `/`, so the head would go
    first. That is the case where a reader can be told a code holds nothing
    while the result that filled it is already being served.
    """
    paths = [
        "browse/2026-08-07/1.json",
        "browse/2026.json",
        "browse/index.json",
        "feed.xml",
        "subjects/msc/11A05.json",
        "subjects/msc/11A05/2026-08-07/1.json",
        "subjects/msc/11A05/2026.json",
    ]
    assert sorted(paths, key=lambda path: (_rank(path), path)) == [
        "browse/2026-08-07/1.json",
        "feed.xml",
        "subjects/msc/11A05/2026-08-07/1.json",
        "browse/2026.json",
        "subjects/msc/11A05/2026.json",
        "browse/index.json",
        "subjects/msc/11A05.json",
    ]
