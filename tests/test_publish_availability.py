"""Source availability is published on its own, in a defined order."""

from __future__ import annotations

import datetime as dt
import json

import pytest
from publish_availability import (
    KEY,
    active_availability,
    publish_availability,
)
from source_availability_contract import MAX_MANIFEST_BYTES, MAX_OBSERVATION_AGE_SECONDS
from test_publish_snapshot import MemoryR2


def _stamp(offset_seconds: float = 0) -> str:
    moment = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=offset_seconds)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _site(tmp_path, name, repositories, generated_at=None):
    site = tmp_path / name
    site.mkdir(parents=True)
    (site / "source-availability.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": generated_at or _stamp(),
                "coverage": {
                    "queries_total": 2 * len(repositories),
                    "planned_refresh_cycle_hours": 0 if not repositories else 6,
                },
                "repositories": repositories,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return site


def _state(checked_at=None, status="available"):
    checked_at = checked_at or _stamp()
    return {
        "status": status,
        "checked_at": checked_at,
        "last_attempt_at": checked_at,
        "consecutive_missing": 0,
        "last_error": None,
    }


def _row(name="example/project", checked_at=None):
    return {
        "source_repository": name,
        "commit": "c" * 40,
        "fork_repository": f"PalomarArchive/{name}",
        "original": _state(checked_at),
        "archive": _state(checked_at),
    }


def test_an_empty_observed_manifest_is_not_mistaken_for_empty_data(db, tmp_path):
    """A file that is there and says nothing is not a manifest saying nothing."""
    target = tmp_path / "observed.json"
    target.write_bytes(b"")

    with pytest.raises(
        ValueError, match="source availability manifest is not valid JSON"
    ):
        active_availability(db.path, target)


def test_the_first_publication_starts_the_order_at_one(tmp_path):
    now = dt.datetime(2026, 8, 8, 12, tzinfo=dt.UTC)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [_row(checked_at=stamp)]
    client = MemoryR2()
    assert publish_availability(
        client,
        "bucket",
        _site(tmp_path, "one", rows, stamp),
        now=now,
    ) == "published"
    live = json.loads(client.objects[KEY]["Body"])
    assert live["publication_revision"] == 1
    assert live["repositories"] == rows


def test_the_same_content_is_not_republished(tmp_path):
    """Otherwise a six-hourly refresh writes an object four times a day to say
    exactly what it said before."""
    first_now = dt.datetime(2026, 8, 8, 12, tzinfo=dt.UTC)
    stamp = first_now.strftime("%Y-%m-%dT%H:%M:%SZ")
    client = MemoryR2()
    rows = [_row(checked_at=stamp)]
    publish_availability(
        client,
        "bucket",
        _site(tmp_path, "one", rows, stamp),
        now=first_now,
    )
    client.put_order.clear()
    assert (
        publish_availability(
            client,
            "bucket",
            _site(tmp_path, "two", rows, stamp),
            now=first_now + dt.timedelta(hours=1),
        )
        == "unchanged"
    )
    assert client.put_order == []


def test_a_takedown_is_published_even_though_the_timestamp_did_not_move(tmp_path):
    """This is why publication order is carried separately from observation
    time: a takedown drops rows while preserving the time they were observed,
    so a rule that read the timestamp would refuse the one write that matters.
    """
    client = MemoryR2()
    stamp = _stamp()
    publish_availability(
        client, "bucket", _site(tmp_path, "one", [_row("a/one"), _row("b/two")], stamp)
    )
    assert publish_availability(
        client, "bucket", _site(tmp_path, "two", [_row("a/one")], stamp)
    ) == "published"
    live = json.loads(client.objects[KEY]["Body"])
    assert [row["source_repository"] for row in live["repositories"]] == ["a/one"]
    assert live["publication_revision"] == 2


def test_a_manifest_the_website_would_discard_is_not_published(tmp_path):
    """Replacing one thing nobody can use with another is not worth a red run."""
    client = MemoryR2()
    publish_availability(client, "bucket", _site(tmp_path, "one", [_row()]))
    before = bytes(client.objects[KEY]["Body"])
    outcome = publish_availability(
        client, "bucket", _site(tmp_path, "old", [_row("c/three")], _stamp(-19 * 3600))
    )
    assert outcome == "stale"
    assert bytes(client.objects[KEY]["Body"]) == before


def test_a_manifest_dated_in_the_future_is_not_published(tmp_path):
    client = MemoryR2()
    outcome = publish_availability(
        client, "bucket", _site(tmp_path, "ahead", [_row()], _stamp(3600))
    )
    assert outcome == "stale"
    assert KEY not in client.objects


def test_an_incomplete_manifest_does_not_replace_the_live_one(tmp_path):
    """A blind provider run knows nothing and must not look freshly healthy."""
    client = MemoryR2()
    publish_availability(client, "bucket", _site(tmp_path, "live", [_row()]))
    before = bytes(client.objects[KEY]["Body"])
    site = _site(tmp_path, "blind", [])
    manifest_path = site / "source-availability.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["coverage"]["budget_exhausted"] = True
    manifest_path.write_text(json.dumps(manifest))

    assert publish_availability(client, "bucket", site) == "incomplete"
    assert bytes(client.objects[KEY]["Body"]) == before


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"", "served source availability manifest is not valid JSON"),
        (
            json.dumps({
                "schema_version": 1,
                "generated_at": _stamp(),
                "coverage": {},
                "repositories": [],
            }).encode(),
            "invalid publication_revision",
        ),
    ],
)
def test_a_malformed_live_manifest_is_not_overwritten_at_revision_one(
    tmp_path, body, message
):
    client = MemoryR2()
    client.objects[KEY] = {"Body": body, "Metadata": {}}

    with pytest.raises(ValueError, match=message):
        publish_availability(client, "bucket", _site(tmp_path, "staged", [_row()]))

    assert client.objects[KEY]["Body"] == body
    assert client.put_order == []


def test_it_never_touches_the_pointer_or_a_release(tmp_path):
    client = MemoryR2()
    publish_availability(client, "bucket", _site(tmp_path, "one", [_row()]))
    assert set(client.objects) == {KEY}


def test_an_unsupported_schema_is_refused(tmp_path):
    client = MemoryR2()
    site = _site(tmp_path, "one", [_row()])
    (site / "source-availability.json").write_text(json.dumps({"schema_version": 99}) + "\n")
    with pytest.raises(ValueError, match="schema"):
        publish_availability(client, "bucket", site)


def test_publish_boundary_derives_refresh_capacity_from_active_rows(tmp_path):
    """A database publication may filter the manifest served before this
    producer revision; population fields are derived rather than trusted."""
    client = MemoryR2()
    site = _site(tmp_path, "old-coverage", [_row()])
    path = site / "source-availability.json"
    manifest = json.loads(path.read_text())
    manifest["coverage"] = {
        "queries": 2,
        "asked": 2,
        "answered": 2,
        "skipped_for_budget": 0,
        "budget_exhausted": False,
    }
    path.write_text(json.dumps(manifest))

    assert publish_availability(client, "bucket", site) == "published"
    coverage = json.loads(client.objects[KEY]["Body"])["coverage"]
    assert coverage["query_budget"] == 800
    assert coverage["queries_total"] == 2
    assert coverage["planned_refresh_cycle_hours"] == 6


def test_stale_known_rows_are_published_as_unknown(tmp_path):
    now = dt.datetime(2026, 8, 8, 12, tzinfo=dt.UTC)
    generated_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    checked_at = (now - dt.timedelta(seconds=MAX_OBSERVATION_AGE_SECONDS + 1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    client = MemoryR2()

    assert publish_availability(
        client,
        "bucket",
        _site(tmp_path, "stale-row", [_row(checked_at=checked_at)], generated_at),
        now=now,
    ) == "published"

    live = json.loads(client.objects[KEY]["Body"])
    assert live["repositories"][0]["original"]["status"] == "unknown"
    assert live["repositories"][0]["archive"]["status"] == "unknown"
    assert live["coverage"]["observations_stale"] == 2
    assert live["coverage"]["observations_fresh"] == 0


@pytest.mark.parametrize("checked_at", [None, "not-a-timestamp"])
def test_missing_or_invalid_known_observations_are_published_as_unknown(
    tmp_path, checked_at
):
    now = dt.datetime(2026, 8, 8, 12, tzinfo=dt.UTC)
    generated_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    row = _row(checked_at=generated_at)
    row["original"]["checked_at"] = checked_at
    client = MemoryR2()

    publish_availability(
        client,
        "bucket",
        _site(tmp_path, "unobserved-row", [row], generated_at),
        now=now,
    )

    original = json.loads(client.objects[KEY]["Body"])["repositories"][0]["original"]
    assert original["status"] == "unknown"
    assert original["checked_at"] is None


def test_the_final_published_bytes_must_fit_the_global_budget(tmp_path):
    client = MemoryR2()
    site = _site(tmp_path, "too-large", [_row()])
    manifest = json.loads((site / "source-availability.json").read_text())
    manifest["padding"] = "x" * MAX_MANIFEST_BYTES
    (site / "source-availability.json").write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match="global delivery budget"):
        publish_availability(client, "bucket", site)
    assert KEY not in client.objects


def test_a_manifest_whose_refresh_cycle_exceeds_freshness_is_not_published(tmp_path):
    client = MemoryR2()
    rows = [
        {
            **_row(name=f"source{index:04d}/project"),
            "fork_repository": f"PalomarArchive/archive{index:04d}",
            "commit": f"{index:040x}",
        }
        for index in range(1_201)
    ]
    site = _site(tmp_path, "too-slow", rows)

    with pytest.raises(ValueError, match="more than 18 hours"):
        publish_availability(client, "bucket", site)
    assert KEY not in client.objects


# Producing the manifest without building a whole release to hold it.


def test_the_active_filter_needs_no_staged_release(db, tmp_path):
    """It used to be a side effect of staging one: twenty-five files an entry,
    copied and re-hashed, four times a day, for one small object that
    cross-references none of them."""
    from check_source_availability import build_manifest
    from publish_availability import active_availability

    db.add_entry("PALOMAR-2026-07-29-000002", 1)
    observed = build_manifest(
        db.path, {}, lambda _repository, _commit: ("available", None),
        generated_at="2026-08-07T00:00:00Z", database_commit="d" * 40,
    )
    manifest = tmp_path / "observed.json"
    manifest.write_text(json.dumps(observed))

    filtered = active_availability(db.path, manifest)

    assert filtered["schema_version"] == 1
    assert filtered["repositories"], "it filtered everything away"
    assert "database_commit" not in filtered
    assert filtered["coverage"] == observed["coverage"], "the evidence came through"
    assert not (tmp_path / "public-data").exists(), "it staged a release"


def test_cli_rejects_the_superseded_staged_release_mode(monkeypatch, capsys):
    import publish_availability as publisher

    monkeypatch.setattr(
        "sys.argv",
        [
            "publish_availability.py",
            "--manifest",
            "observed.json",
            "--site",
            "somewhere",
        ],
    )
    with pytest.raises(SystemExit) as stopped:
        publisher.main()
    assert stopped.value.code == 2
    assert "unrecognized arguments: --site" in capsys.readouterr().err


def test_a_withdrawn_record_leaves_the_filtered_manifest(db, tmp_path):
    from check_source_availability import build_manifest
    from publish_availability import active_availability

    entry = db.entry_data("PALOMAR-2026-07-29-000002", 1)
    entry["source"].update({
        "repository": "example/withdrawn-source",
        "repository_url": "https://github.com/example/withdrawn-source",
        "commit": "a" * 40,
        "tree_url": "https://github.com/example/withdrawn-source/tree/" + "a" * 40,
    })
    installed = db.install_entry(entry)
    source_mapping = next(
        row
        for row in entry["preservation"]["repositories"]
        if row["source_repository"] == "example/withdrawn-source"
        and row["commit"] == "a" * 40
    )
    source_mapping["fork_repository"] = "PalomarArchive/withdrawn-source"
    db.write_json(installed.relative_to(db.path).as_posix(), entry)
    withdrawn_mapping = (
        "example/withdrawn-source",
        "a" * 40,
        "PalomarArchive/withdrawn-source",
    )
    observed = build_manifest(
        db.path, {}, lambda _repository, _commit: ("available", None),
        generated_at="2026-08-07T00:00:00Z", database_commit="d" * 40,
    )
    manifest = tmp_path / "observed.json"
    manifest.write_text(json.dumps(observed))
    before = active_availability(db.path, manifest)
    before_mappings = {
        (row["source_repository"], row["commit"], row["fork_repository"])
        for row in before["repositories"]
    }
    assert withdrawn_mapping in before_mappings

    db.write_json("takedowns.json", {
        "schema_version": 1,
        "takedowns": [{
            "id": "PALOMAR-2026-07-29-000002", "version": 1,
            "taken_down_at": "2026-08-06T12:00:00Z",
            "authorized_by_login": "avigad", "authorization_issue": 101, "reason": "a private reason",
        }],
    })
    after = active_availability(db.path, manifest)
    after_mappings = {
        (row["source_repository"], row["commit"], row["fork_repository"])
        for row in after["repositories"]
    }
    assert after_mappings == before_mappings - {withdrawn_mapping}
    assert after["coverage"]["observations_total"] == 2 * len(after["repositories"])
    assert after["coverage"]["observations_fresh"] == 2 * len(after["repositories"])
