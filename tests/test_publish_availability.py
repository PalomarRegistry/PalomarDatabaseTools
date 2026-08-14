"""Source availability is published on its own, in a defined order."""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import sys
import textwrap

import pytest
from publish_availability import (
    KEY,
    active_availability,
    publish_availability,
    validate_retained_availability,
    write_current_availability,
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


def test_reading_an_absent_current_manifest_leaves_no_file(tmp_path):
    target = tmp_path / "current.json"
    target.write_text("stale bytes from an earlier attempt")

    assert not write_current_availability(MemoryR2(), "bucket", target)
    assert not target.exists()


def test_reading_a_current_manifest_preserves_its_exact_bytes(tmp_path):
    client = MemoryR2()
    body = b'{"schema_version":1,"repositories":[]}\n'
    client.objects[KEY] = {"Body": body, "Metadata": {}}
    target = tmp_path / "current.json"
    target.write_bytes(b"an older complete handoff\n")

    assert write_current_availability(client, "bucket", target)
    assert target.read_bytes() == body
    assert list(tmp_path.iterdir()) == [target]


def test_a_failed_atomic_write_removes_its_pending_file(tmp_path, monkeypatch):
    import publish_availability as module

    client = MemoryR2()
    client.objects[KEY] = {"Body": b"new bytes\n", "Metadata": {}}
    target = tmp_path / "current.json"
    target.write_bytes(b"the complete earlier handoff\n")
    original = module.tempfile.NamedTemporaryFile

    class FailingWrite:
        def __init__(self, **arguments):
            self.stream = original(**arguments)
            self.name = self.stream.name

        def __enter__(self):
            return self

        def __exit__(self, *reason):
            self.stream.close()

        def write(self, _raw):
            raise OSError("the temporary write failed")

    monkeypatch.setattr(module.tempfile, "NamedTemporaryFile", FailingWrite)

    with pytest.raises(OSError, match="temporary write failed"):
        write_current_availability(client, "bucket", target)

    assert target.read_bytes() == b"the complete earlier handoff\n"
    assert list(tmp_path.iterdir()) == [target]


def test_an_existing_empty_object_is_not_mistaken_for_a_cold_bucket(db, tmp_path):
    client = MemoryR2()
    client.objects[KEY] = {"Body": b"", "Metadata": {}}
    target = tmp_path / "current.json"

    assert write_current_availability(client, "bucket", target)
    assert target.exists()
    with pytest.raises(
        ValueError, match="source availability manifest is not valid JSON"
    ):
        active_availability(db.path, target)


def test_a_retained_manifest_is_validated_without_opening_the_database(tmp_path):
    path = _site(tmp_path, "retained", [_row()]) / "source-availability.json"
    document = json.loads(path.read_text())
    document["publication_revision"] = 4
    path.write_text(json.dumps(document) + "\n")

    validate_retained_availability(path)
    root = pathlib.Path(__file__).resolve().parents[1]
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {"CLOUDFLARE_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"}
    }
    result = subprocess.run(
        [
            sys.executable,
            os.fspath(root / "tools/publish_availability.py"),
            "--validate-retained",
            os.fspath(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    assert "retained source availability is valid" in result.stdout


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda _document: "{", "not valid JSON"),
        (
            lambda document: {**document, "publication_revision": True},
            "invalid publication_revision",
        ),
        (
            lambda document: {**document, "generated_at": "not-a-time"},
            "malformed generated_at",
        ),
        (
            lambda document: {**document, "repositories": [{}]},
            "repositories\\[0\\].source_repository",
        ),
        (
            lambda document: {
                **document,
                "coverage": {"query_budget": 0},
                "repositories": [_row()],
            },
            "more than 18 hours",
        ),
    ],
)
def test_a_malformed_retained_manifest_fails_closed(tmp_path, mutate, message):
    path = _site(tmp_path, "retained-bad", []) / "source-availability.json"
    document = json.loads(path.read_text())
    document["publication_revision"] = 1
    changed = mutate(document)
    path.write_text(changed if isinstance(changed, str) else json.dumps(changed))

    with pytest.raises(ValueError, match=message):
        validate_retained_availability(path)


def _workflow_script(workflow: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^      - name: {re.escape(name)}\n.*?(?=^      - (?:name|uses):|\Z)",
        workflow,
    )
    assert match is not None, f"workflow has no step named {name!r}"
    lines = match.group(0).splitlines()
    single = next(
        (line.removeprefix("        run: ") for line in lines if line.startswith("        run: ")),
        None,
    )
    if single is not None and single != "|":
        return single + "\n"
    start = lines.index("        run: |") + 1
    body = []
    for line in lines[start:]:
        if line and not line.startswith("          "):
            break
        body.append(line[10:] if line else "")
    assert body, f"workflow step {name!r} has no shell block"
    return "\n".join(body).rstrip() + "\n"


def test_workflow_script_stops_before_yaml_comments_and_the_next_uses_step():
    workflow = """jobs:
  publish:
    steps:
      - name: Example
        run: |
          echo one
          echo two
      # This is YAML, not shell.
      - uses: example/action@commit
"""

    assert _workflow_script(workflow, "Example") == "echo one\necho two\n"


def test_workflow_script_reads_a_single_line_run():
    workflow = """jobs:
  check:
    steps:
      - name: Example
        run: python tool.py --input \"$RUNNER_TEMP/input.json\"
      - uses: example/action@commit
"""

    assert _workflow_script(workflow, "Example") == (
        'python tool.py --input "$RUNNER_TEMP/input.json"\n'
    )


def _availability_workflow_scripts(root: pathlib.Path) -> list[str]:
    workflow = (root / ".github/workflows/publish.yml").read_text()
    load = _workflow_script(workflow, "Load current operational source availability")
    publish = _workflow_script(workflow, "Publish availability after activation")
    loaded = re.search(r'--write-current\s+("\$RUNNER_TEMP/[^"]+")', load)
    consumed = re.search(r'--manifest\s+("\$RUNNER_TEMP/[^"]+")', publish)
    guarded = re.search(r'if \[ -f ("\$RUNNER_TEMP/[^"]+") \]; then', publish)
    assert loaded and consumed and guarded
    assert loaded.group(1) == consumed.group(1) == guarded.group(1)
    return [load, publish]


@pytest.mark.skipif(
    not (pathlib.Path(__file__).resolve().parents[1] / ".github/workflows/publish.yml").is_file(),
    reason="private database workflow contract",
)
def test_accepted_publication_validates_the_exact_retained_handoff_without_secrets():
    root = pathlib.Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/publish.yml").read_text()
    load = _workflow_script(workflow, "Load current operational source availability")
    validate = _workflow_script(
        workflow, "Validate availability retained by an accepted delta"
    )
    written = re.search(r'--write-current\s+("\$RUNNER_TEMP/[^"]+")', load)
    retained = re.search(r'--validate-retained\s+("\$RUNNER_TEMP/[^"]+")', validate)

    assert written and retained and written.group(1) == retained.group(1)
    assert "CLOUDFLARE_ACCOUNT_ID" not in validate
    assert "R2_ACCESS_KEY_ID" not in validate


@pytest.mark.skipif(
    not (pathlib.Path(__file__).resolve().parents[1] / ".github/workflows/publish.yml").is_file(),
    reason="private database workflow contract",
)
def test_source_availability_workflow_uses_one_previous_manifest_handoff():
    root = pathlib.Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/source-availability.yml").read_text()
    load = _workflow_script(workflow, "Read the availability state being served")
    refresh = _workflow_script(workflow, "Refresh the bounded oldest source observations")
    written = re.search(r'--write-current\s+("\$RUNNER_TEMP/[^"]+")', load)
    consumed = re.search(r'--previous\s+("\$RUNNER_TEMP/[^"]+")', refresh)

    assert written and consumed
    assert written.group(1) == consumed.group(1)


def _fake_r2_module(imports: pathlib.Path) -> None:
    imports.mkdir()
    (imports / "boto3.py").write_text(
        textwrap.dedent(
            """
            import io
            import json
            import os
            import pathlib

            body_path = pathlib.Path(os.environ["FAKE_R2_BODY"])
            metadata_path = pathlib.Path(os.environ["FAKE_R2_METADATA"])
            puts_path = pathlib.Path(os.environ["FAKE_R2_PUTS"])

            class Missing(Exception):
                response = {"Error": {"Code": "NoSuchKey"}}

            class Bucket:
                def head_object(self, *, Bucket, Key):
                    if not body_path.is_file():
                        raise Missing()
                    metadata = (
                        json.loads(metadata_path.read_text())
                        if metadata_path.is_file()
                        else {}
                    )
                    return {
                        "ContentLength": len(body_path.read_bytes()),
                        "Metadata": metadata,
                    }

                def get_object(self, *, Bucket, Key):
                    if not body_path.is_file():
                        raise Missing()
                    metadata = (
                        json.loads(metadata_path.read_text())
                        if metadata_path.is_file()
                        else {}
                    )
                    return {
                        "Body": io.BytesIO(body_path.read_bytes()),
                        "Metadata": metadata,
                    }

                def put_object(self, *, Bucket, Key, Body, Metadata, **kwargs):
                    body_path.write_bytes(bytes(Body))
                    metadata_path.write_text(json.dumps(Metadata))
                    with puts_path.open("a") as stream:
                        stream.write(Key + "\\n")

            def client(*args, **kwargs):
                return Bucket()
            """
        )
    )


def _run_availability_workflow(
    tmp_path, initial: bytes | None
) -> tuple[list[str], pathlib.Path, pathlib.Path]:
    root = pathlib.Path(__file__).resolve().parents[1]
    scripts = _availability_workflow_scripts(root)
    imports = tmp_path / "imports"
    _fake_r2_module(imports)
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    body = tmp_path / "r2-body"
    if initial is not None:
        body.write_bytes(initial)
    environment = {
        **os.environ,
        "RUNNER_TEMP": str(runner_temp),
        "PYTHONPATH": os.pathsep.join([str(imports), str(root / "tools")]),
        "CLOUDFLARE_ACCOUNT_ID": "test",
        "R2_ACCESS_KEY_ID": "test",
        "R2_SECRET_ACCESS_KEY": "test",
        "FAKE_R2_BODY": str(body),
        "FAKE_R2_METADATA": str(tmp_path / "r2-metadata"),
        "FAKE_R2_PUTS": str(tmp_path / "r2-puts"),
    }

    output = []
    for script in scripts:
        result = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", script],
            cwd=root,
            env=environment,
            text=True,
            capture_output=True,
            check=True,
        )
        output.append(result.stdout)
    return output, body, runner_temp


@pytest.mark.skipif(
    not (pathlib.Path(__file__).resolve().parents[1] / ".github/workflows/publish.yml").is_file(),
    reason="private database workflow contract",
)
def test_cold_bucket_database_publication_leaves_availability_absent(tmp_path):
    """Execute the two availability workflow steps against an empty bucket.

    The release has already activated when the second step runs. An absent R2
    object therefore must not become an empty file that the publisher tries to
    JSON-decode, turning the first otherwise-successful publication red.
    """
    output, body, runner_temp = _run_availability_workflow(tmp_path, None)

    assert not body.exists()
    assert not (runner_temp / "source-availability.json").exists()
    assert "nothing is served yet" in output[0]
    assert "leaving it absent" in output[1]


@pytest.mark.skipif(
    not (pathlib.Path(__file__).resolve().parents[1] / ".github/workflows/publish.yml").is_file(),
    reason="private database workflow contract",
)
def test_warm_bucket_database_publication_executes_the_production_publish_path(
    tmp_path,
):
    manifest = {
        "schema_version": 1,
        "generated_at": _stamp(),
        "coverage": {"query_budget": 800},
        "repositories": [],
        "publication_revision": 7,
    }

    output, body, runner_temp = _run_availability_workflow(
        tmp_path, json.dumps(manifest).encode() + b"\n"
    )

    assert "read the manifest being served" in output[0]
    assert "published source availability revision 8" in output[1]
    assert (runner_temp / "source-availability.json").is_file()
    assert json.loads(body.read_bytes())["publication_revision"] == 8
    assert (tmp_path / "r2-puts").read_text().splitlines() == [KEY]


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
