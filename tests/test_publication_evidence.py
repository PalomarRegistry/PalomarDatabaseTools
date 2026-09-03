"""The release-specific handoff from publication to health."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))

import publication_evidence  # noqa: E402
import release_delta  # noqa: E402
from stage_public import FULL_REQUIRED_EXIT, stage_public  # noqa: E402
from validate import FULL_CHECKOUT_EXIT  # noqa: E402

COMMIT = "1" * 40
OTHER_COMMIT = "2" * 40
FIRST = "PALOMAR-2026-08-08-000001-v1"
SECOND = "PALOMAR-2026-08-08-000002-v2"
FIXTURE_FIRST = "PALOMAR-2026-07-29-000001-v1"
FIXTURE_SECOND_ID = "PALOMAR-2026-07-29-000002"


def _row(path: str) -> dict[str, object]:
    return {"path": path, "bytes": 1, "sha256": "a" * 64}


def _write_delta(
    path: pathlib.Path,
    *,
    commit: str = COMMIT,
    target: str = FIRST,
    withdrawn: str = SECOND,
) -> dict:
    delta = {
        "schema_version": release_delta.DELTA_SCHEMA,
        "surfaces": release_delta.SURFACES,
        "parent": None,
        "database_commit": commit,
        "additions": [
            _row(f"entries/{target}.json"),
            _row(f"evidence/{target}/{'c' * 64}/report.json"),
            _row(f"renders/{target}/{'d' * 64}/Challenge/index.html"),
        ],
        "withdrawals": [],
        "retired": [],
        "stable": [_row(f"tombstones/{withdrawn}.json")],
        "aggregates": [],
        "takedowns_git_blob": "f" * 40,
        "records": {"count": 2, "root": "e" * 64},
    }
    path.write_bytes(release_delta.canonical_bytes(delta))
    return delta


def _write_record(directory: pathlib.Path, target: str) -> bytes:
    directory.mkdir(parents=True, exist_ok=True)
    identifier, version = target.rsplit("-v", 1)
    path = directory / f"{target}.json"
    path.write_text(json.dumps({
        "id": identifier,
        "version": int(version),
        "challenge_render": {
            "artifact_path": f"renders/{target}/{'a' * 64}/",
            "entrypoint": "Challenge/index.html",
        },
    }))
    return path.read_bytes()


def _prepared(
    root: pathlib.Path,
    *,
    commit: str = COMMIT,
    target: str = FIRST,
    withdrawn: str = SECOND,
):
    delta_path = root / "release-delta.json"
    entries = root / "entries"
    bundled = root / "health-entries"
    target_bytes = _write_record(entries, target)
    _write_record(entries, withdrawn)
    delta = _write_delta(
        delta_path, commit=commit, target=target, withdrawn=withdrawn
    )
    entry_row = next(
        row for row in delta["additions"] if row["path"].startswith("entries/")
    )
    entry_row["bytes"] = len(target_bytes)
    entry_row["sha256"] = hashlib.sha256(target_bytes).hexdigest()
    delta_path.write_bytes(release_delta.canonical_bytes(delta))
    publication_evidence.prepare(
        delta_path,
        commit=commit,
        entries=entries,
        bundle_entries=bundled,
    )
    return delta_path, bundled, delta


def test_prepare_and_verify_validate_commit_and_exact_versions(tmp_path):
    delta_path, bundled, _delta = _prepared(tmp_path)

    versions, withdrawn = publication_evidence.verify(
        delta_path, commit=COMMIT, entries=bundled
    )

    assert versions == [FIRST]
    assert withdrawn == [SECOND]
    assert sorted(path.name for path in bundled.iterdir()) == [
        f"{FIRST}.json",
        f"{SECOND}.json",
    ]


def test_out_of_order_events_keep_their_own_triggering_commit(tmp_path):
    first = _prepared(tmp_path / "first")
    second = _prepared(
        tmp_path / "second",
        commit=OTHER_COMMIT,
        target=SECOND,
        withdrawn=FIRST,
    )

    # Each event verifies independently however the jobs happen to be ordered.
    assert publication_evidence.verify(
        second[0], commit=OTHER_COMMIT, entries=second[1]
    )[0] == [SECOND]
    assert publication_evidence.verify(
        first[0], commit=COMMIT, entries=first[1]
    )[0] == [FIRST]
    with pytest.raises(ValueError, match="different triggering commit"):
        publication_evidence.verify(
            first[0], commit=OTHER_COMMIT, entries=first[1]
        )


def test_a_noncanonical_or_malformed_delta_fails_closed(tmp_path):
    delta_path, bundled, delta = _prepared(tmp_path)
    delta_path.write_text(json.dumps(delta), encoding="utf-8")

    with pytest.raises(ValueError, match="canonical byte form"):
        publication_evidence.verify(delta_path, commit=COMMIT, entries=bundled)


def test_a_historical_immutable_addition_requires_canonical_git(tmp_path):
    delta_path = tmp_path / "release-delta.json"
    delta = _write_delta(delta_path)
    delta["additions"].append(
        _row(f"renders/PALOMAR-2026-08-08-999999-v1/{'f' * 64}/index.html")
    )
    delta["additions"] = sorted(delta["additions"], key=lambda row: row["path"])
    delta_path.write_bytes(release_delta.canonical_bytes(delta))

    first_raw = _write_record(tmp_path / "entries", FIRST)
    first_row = next(
        row for row in delta["additions"] if row["path"] == f"entries/{FIRST}.json"
    )
    first_row["bytes"] = len(first_raw)
    first_row["sha256"] = hashlib.sha256(first_raw).hexdigest()
    delta_path.write_bytes(release_delta.canonical_bytes(delta))
    _write_record(tmp_path / "entries", "PALOMAR-2026-08-08-999999-v1")
    _write_record(tmp_path / "entries", SECOND)
    with pytest.raises(ValueError, match="needs the canonical Git checkout"):
        publication_evidence.prepare(
            delta_path,
            commit=COMMIT,
            entries=tmp_path / "entries",
            bundle_entries=tmp_path / "health-entries",
        )


def test_historical_additions_are_bound_to_the_committed_entry(repo, tmp_path):
    historical = FIRST
    current = SECOND
    historical_id, historical_version = historical.rsplit("-v", 1)
    current_id, current_version = current.rsplit("-v", 1)
    repo.add_entry(historical_id, int(historical_version))
    repo.add_entry(current_id, int(current_version))
    commit = repo.commit("current entry and historical correction dependency")

    delta_path = tmp_path / "release-delta.json"
    delta = _write_delta(delta_path, commit=commit, target=current)
    delta["stable"] = []
    current_raw = (repo.path / f"entries/{current}.json").read_bytes()
    current_row = next(
        row for row in delta["additions"] if row["path"] == f"entries/{current}.json"
    )
    current_row["bytes"] = len(current_raw)
    current_row["sha256"] = hashlib.sha256(current_raw).hexdigest()
    delta["additions"].append(
        _row(f"evidence/{historical}/{'f' * 64}/evidence-manifest.json")
    )
    delta["additions"] = sorted(delta["additions"], key=lambda row: row["path"])
    delta_path.write_bytes(release_delta.canonical_bytes(delta))
    bundled = tmp_path / "health-entries"

    publication_evidence.prepare(
        delta_path,
        database=repo.path,
        commit=commit,
        entries=repo.path / "entries",
        bundle_entries=bundled,
    )
    assert publication_evidence.verify(
        delta_path,
        commit=commit,
        entries=bundled,
        database=repo.path,
    ) == ([historical, current], [])

    (bundled / f"{historical}.json").write_bytes(current_raw)
    with pytest.raises(ValueError, match="disagrees with its path|disagrees with canonical Git"):
        publication_evidence.verify(
            delta_path,
            commit=commit,
            entries=bundled,
            database=repo.path,
        )


def _stage_real(repo, tmp_path, name, previous=None):
    served = tmp_path / "served"
    served.mkdir(exist_ok=True)
    site = tmp_path / name
    stage_public(
        repo.path,
        site,
        previous=(None if previous is None else release_delta.base_of(previous)),
        prior=served,
    )
    delta = release_delta.parse((site / "release-delta.json").read_bytes())
    (site / release_delta.BASE_PATH).write_bytes(
        release_delta.canonical_base_bytes(release_delta.base_of(delta))
    )
    shutil.copytree(site, served, dirs_exist_ok=True)
    return site, delta


def _prepare_real(repo, site, commit, tmp_path, name, *, previous=None):
    bundled = tmp_path / f"{name}-health-entries"
    delta_path = site / "release-delta.json"
    publication_evidence.prepare(
        delta_path,
        previous_base=(
            None if previous is None else previous / release_delta.BASE_PATH
        ),
        database=repo.path,
        commit=commit,
        entries=repo.path / "entries",
        bundle_entries=bundled,
    )
    return publication_evidence.verify(delta_path, commit=commit, entries=bundled)


def test_prepare_consumes_real_full_incremental_empty_and_takedown_deltas(
    repo, tmp_path
):
    """Exercise the producer's actual deltas, not only hand-built examples."""
    first_commit = repo.git("rev-parse", "HEAD").strip()
    full_site, full = _stage_real(repo, tmp_path, "full")
    assert full["parent"] is None
    assert _prepare_real(repo, full_site, first_commit, tmp_path, "full") == (
        [FIXTURE_FIRST],
        [],
    )

    repo.add_entry(FIXTURE_SECOND_ID, 1)
    second_commit = repo.commit("add a second record")
    incremental_site, incremental = _stage_real(
        repo, tmp_path, "incremental", previous=full
    )
    assert incremental["parent"] is not None
    assert _prepare_real(
        repo,
        incremental_site,
        second_commit,
        tmp_path,
        "incremental",
        previous=full_site,
    ) == ([f"{FIXTURE_SECOND_ID}-v1"], [])

    empty_commit = repo.commit("publication with no record changes")
    empty_site, empty = _stage_real(repo, tmp_path, "empty", previous=incremental)
    assert empty["parent"] is not None
    assert empty["additions"] == []
    assert _prepare_real(
        repo,
        empty_site,
        empty_commit,
        tmp_path,
        "empty",
        previous=incremental_site,
    ) == ([], [])
    # A bot recovery may produce exactly this empty delta after the original
    # release partly failed. Valid evidence does not make it eligible for the
    # narrow mode: only a fresh current-main full check can close recovery.
    assert publication_evidence.choose_mode(
        "workflow_run",
        "success",
        "success",
        "success",
        "workflow_dispatch",
        "github-actions[bot]",
    ) == "full"

    repo.write_json(
        "takedowns.json",
        {
            "schema_version": 1,
            "takedowns": [
                {
                    "id": FIXTURE_SECOND_ID,
                    "version": 1,
                    "taken_down_at": "2026-08-08T12:00:00Z",
                    "authorized_by_login": "avigad",
                    "authorization_issue": 101,
                    "reason": "integration fixture",
                }
            ],
        },
    )
    takedown_commit = repo.commit("withdraw the second record")
    takedown_site, takedown = _stage_real(
        repo, tmp_path, "takedown", previous=empty
    )
    assert _prepare_real(
        repo, takedown_site, takedown_commit, tmp_path, "takedown"
    ) == ([FIXTURE_FIRST], [f"{FIXTURE_SECOND_ID}-v1"])


def test_incremental_health_requires_the_exact_parent_takedown_authority(repo, tmp_path):
    parent_site, parent = _stage_real(repo, tmp_path, "parent")
    repo.add_entry(FIXTURE_SECOND_ID, 1)
    commit = repo.commit("add one record")
    current_site, current = _stage_real(
        repo, tmp_path, "current", previous=parent
    )
    delta_path = current_site / "release-delta.json"

    with pytest.raises(ValueError, match="needs its served parent"):
        publication_evidence.prepare(
            delta_path,
            commit=commit,
            entries=repo.path / "entries",
            bundle_entries=tmp_path / "missing-parent-health",
        )

    current["takedowns_git_blob"] = "f" * 40
    delta_path.write_bytes(release_delta.canonical_bytes(current))
    with pytest.raises(ValueError, match="authority differs"):
        publication_evidence.prepare(
            delta_path,
            previous_base=parent_site / release_delta.BASE_PATH,
            database=repo.path,
            commit=commit,
            entries=repo.path / "entries",
            bundle_entries=tmp_path / "changed-takedowns-health",
        )


@pytest.mark.parametrize(
    (
        "event",
        "conclusion",
        "download",
        "evidence",
        "publication_event",
        "actor",
        "expected",
    ),
    [
        ("workflow_run", "success", "success", "success", "push", "alice", "delta"),
        (
            "workflow_run",
            "success",
            "success",
            "success",
            "workflow_dispatch",
            "alice",
            "delta",
        ),
        (
            "workflow_run",
            "success",
            "success",
            "success",
            "workflow_dispatch",
            "github-actions[bot]",
            "full",
        ),
        ("workflow_run", "failure", "skipped", "skipped", "push", "alice", "full"),
        ("workflow_run", "success", "failure", "skipped", "push", "alice", "full"),
        ("workflow_run", "success", "success", "failure", "push", "alice", "full"),
        ("schedule", "", "skipped", "skipped", "", "", "full"),
        ("workflow_dispatch", "", "skipped", "skipped", "", "alice", "full"),
    ],
)
def test_only_a_fully_valid_run_selected_success_uses_delta_mode(
    event, conclusion, download, evidence, publication_event, actor, expected
):
    assert publication_evidence.choose_mode(
        event,
        conclusion,
        download,
        evidence,
        publication_event,
        actor,
    ) == expected


def test_verify_cli_writes_only_safe_line_oriented_inputs(tmp_path):
    delta_path, bundled, _delta = _prepared(tmp_path)
    versions = tmp_path / "versions.txt"
    withdrawn = tmp_path / "withdrawn.txt"
    result = subprocess.run(
        [
            sys.executable,
            str(pathlib.Path(publication_evidence.__file__)),
            "verify",
            "--delta",
            str(delta_path),
            "--commit",
            COMMIT,
            "--entries",
            str(bundled),
            "--versions",
            str(versions),
            "--withdrawn",
            str(withdrawn),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert versions.read_text() == f"{FIRST}\n"
    assert withdrawn.read_text() == f"{SECOND}\n"


def test_extra_or_changed_bundled_records_are_rejected(tmp_path):
    delta_path, bundled, _delta = _prepared(tmp_path)
    (bundled / "unrelated.json").write_text("{}")
    with pytest.raises(ValueError, match="wrong set"):
        publication_evidence.verify(delta_path, commit=COMMIT, entries=bundled)


def _workflow_step(workflow: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^      - name: {re.escape(name)}\n.*?(?=^      - (?:name|uses):|\Z)",
        workflow,
    )
    assert match is not None, f"workflow has no step named {name!r}"
    return match.group(0)


def _workflow_script(workflow: str, name: str) -> str:
    lines = _workflow_step(workflow, name).splitlines()
    start = lines.index("        run: |") + 1
    body = []
    for line in lines[start:]:
        if line and not line.startswith("          "):
            break
        body.append(line[10:] if line else "")
    assert body, f"workflow step {name!r} has no shell block"
    return "\n".join(body).rstrip() + "\n"


def test_workflows_pin_the_handoff_and_keep_whole_sweeps_off_the_success_path():
    root = pathlib.Path(__file__).resolve().parents[1]
    publish = (root / ".github/workflows/publish.yml").read_text()
    health = (root / ".github/workflows/publish-health.yml").read_text()

    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in publish
    assert "retention-days: 1" in publish
    assert "publication-evidence-${{ github.run_id }}-${{ github.run_attempt }}" in publish
    assert "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c" in health
    assert "run-id: ${{ github.event.workflow_run.id }}" in health
    assert "github.event.workflow_run.run_attempt" in health
    assert "filter: tree:0" in health
    assert "steps.mode.outputs.mode == 'full'" in health
    assert "steps.delta_check.outputs.missing == 'true'" in health
    assert "python tools/publish_snapshot.py --audit" in health
    assert "python tools/check_published.py \"${args[@]}\"" in health


def test_pull_request_validation_scopes_current_and_trusted_base_policy():
    root = pathlib.Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/validate.yml").read_text()
    current = _workflow_step(workflow, "Validate entries and registration projections")
    trusted_base = _workflow_step(workflow, "Validate with the base revision policy")
    trusted_base_script = _workflow_script(
        workflow, "Validate with the base revision policy"
    )
    real_tree = _workflow_step(
        workflow, "Stage the real tree after publication code changes"
    )

    assert 'python tools/validate.py --since "$BASE_SHA"' in current
    assert 'git archive "$BASE_SHA" tools' in trusted_base
    assert 'tar -x -C "$validator_dir" --strip-components=1' in trusted_base
    assert 'python "$validator_dir/validate.py" --root . --since "$BASE_SHA"' in trusted_base
    assert "PYTHONPATH=tools" not in trusted_base_script
    assert "index.json" not in trusted_base_script
    assert "git worktree" not in trusted_base_script
    assert 'git diff --quiet "$BASE_SHA"' in real_tree
    assert "tools/ requirements-tools.txt" in real_tree
    assert "schema-v3.json scores-v1.json takedowns.json" in real_tree
    assert "tests/path-classes.json" in real_tree
    assert "'.github/workflows/publish*.yml'" in real_tree
    assert 'stage_public.py --output "$RUNNER_TEMP/public-data" --full' in real_tree


def test_trusted_base_policy_shell_validates_the_current_checkout(tmp_path):
    root = pathlib.Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/validate.yml").read_text()
    script = _workflow_script(workflow, "Validate with the base revision policy")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    environment = os.environ.copy()
    environment.update({"BASE_SHA": base, "RUNNER_TEMP": str(tmp_path)})
    environment["PATH"] = (
        f"{pathlib.Path(sys.executable).parent}{os.pathsep}{environment['PATH']}"
    )

    result = subprocess.run(
        ["bash", "-c", script],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "database is valid" in result.stdout


def test_extracted_policy_changes_run_the_registration_contract():
    root = pathlib.Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/reviewer-contract.yml").read_text()

    assert workflow.count('- "tools/bundle_reference_validation.py"') == 2
    assert workflow.count('- "tools/entry_validation.py"') == 2
    assert workflow.count('- "tools/evidence_validation.py"') == 2
    assert workflow.count('- "tools/render_validation.py"') == 2
    assert workflow.count('- "tools/score_validation.py"') == 2
    assert workflow.count('- "tools/schema_policy.py"') == 2
    assert workflow.count('- "tools/registration_projection.py"') == 2
    assert workflow.count('- "tools/validation_scope.py"') == 2


def test_schema_policy_changes_trigger_every_production_consumer():
    root = pathlib.Path(__file__).resolve().parents[1]
    publish = (root / ".github/workflows/publish.yml").read_text()
    availability = (root / ".github/workflows/source-availability.yml").read_text()

    assert '- "tools/entry_validation.py"' in publish
    assert '- "tools/schema_policy.py"' in publish
    for path in (
        "schema-v3.json",
        "scores-v1.json",
        "tools/entry_validation.py",
        "tools/schema_policy.py",
        "tools/score_validation.py",
        "tools/smoke_source_availability.py",
    ):
        assert f'- "{path}"' in availability


def test_worker_dependency_audit_runs_on_changes_and_without_changes():
    root = pathlib.Path(__file__).resolve().parents[1]
    package = json.loads((root / "worker/package.json").read_text())
    validate = (root / ".github/workflows/validate.yml").read_text()
    weekly = (root / ".github/workflows/whole-database-sweep.yml").read_text()

    assert package["scripts"]["audit:dependencies"] == "npm audit --audit-level=low"
    assert "npm run audit:dependencies" in validate
    assert "npm run audit:dependencies" in weekly
    assert 'cron: "23 4 * * 1"' in weekly
    assert "workflow_dispatch:" in weekly
    assert "worker-dependencies:" in weekly
    worker_job = weekly.split("  worker-dependencies:\n", 1)[1].split("\n  sweep:\n", 1)[0]
    assert "timeout-minutes: 10" in worker_job
    assert "persist-credentials: false" in worker_job
    assert "secrets." not in worker_job


def test_publication_validates_from_the_release_it_will_patch():
    root = pathlib.Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/publish.yml").read_text()
    read = _workflow_step(workflow, "Read the release base currently being served")
    select = _workflow_step(workflow, "Select the served release validation base")
    checkout_plan = _workflow_step(workflow, "Plan publication checkout")
    materialize = _workflow_step(
        workflow, "Materialize accepted delta or expand full checkout"
    )
    validate = _workflow_step(workflow, "Validate the accepted delta")
    append_only = _workflow_step(workflow, "Recheck append-only canonical paths")
    evidence = _workflow_step(
        workflow, "Describe the activated release for health"
    )
    plan = _workflow_step(
        workflow, "Work out which published documents this release patches"
    )
    page_plan = _workflow_step(
        workflow, "Work out which postings pages those heads point at"
    )

    previous = '"$RUNNER_TEMP/previous-release-base.json"'
    assert "fetch-depth: 0" in workflow
    assert "filter: blob:none" in workflow
    assert "persist-credentials: false" in workflow
    assert "sparse-checkout-cone-mode: false" in workflow
    for pattern in (
        "/tools/",
        "/requirements-tools.txt",
        "/schema-v3.json",
        "/scores-v1.json",
        "/LICENSE",
        "/.palomar-launched",
        "/.github/workflows/publish.yml",
    ):
        assert pattern in workflow
    assert '"$GITHUB_EVENT_NAME" = "workflow_dispatch"' in checkout_plan
    assert '--sparse-paths "$accepted"' in checkout_plan
    assert f"--previous-base {previous}" in checkout_plan
    assert f"{FULL_CHECKOUT_EXIT}) mode=full" in checkout_plan
    assert "GITHUB_TOKEN: ${{ github.token }}" in materialize
    assert "GIT_CONFIG_KEY_0=http.https://github.com/.extraheader" in materialize
    assert 'GIT_CONFIG_VALUE_0="$auth_header"' in materialize
    assert "git -c" not in materialize
    assert 'git ls-tree -z HEAD -- ":(top,literal)$relative"' in materialize
    assert 'git ls-tree -z "$PUBLICATION_BASE" --' in materialize
    assert 'mapfile -d \'\' -t rows < "$metadata"' in materialize
    assert '[ "${#rows[@]}" -eq 0 ]' in materialize
    assert '[ "$mode" != "100644" ]' in materialize
    assert 'git_with_auth cat-file blob "$object_id" > /dev/null' in materialize
    assert "git update-index --no-skip-worktree -z --stdin" in materialize
    assert "git_with_auth checkout-index --force -z --stdin" in materialize
    assert "git_with_auth sparse-checkout disable" in materialize
    assert "python" not in materialize
    assert "--write-current-base" in read and previous in read
    assert "tools/release_delta.py" in select and previous in select
    assert "steps.served_base.outputs.commit" in validate
    assert 'validate.py --since "$PUBLICATION_BASE"' in validate
    assert "github.event.before" not in validate
    assert "steps.served_base.outputs.commit" in append_only
    assert '--base "$PUBLICATION_BASE" --head HEAD' in append_only
    assert "--history HEAD" in append_only and "--tree HEAD" in append_only
    assert previous in plan
    for planning in (plan, page_plan):
        assert "PUBLICATION_MODE: ${{ steps.checkout_plan.outputs.mode }}" in planning
        assert 'if [ "$PUBLICATION_MODE" = "full" ]' in planning
        assert "args+=(--full)" in planning
    assert f"--previous-base {previous}" in evidence
    assert workflow.index(read) < workflow.index(select) < workflow.index(checkout_plan)
    assert workflow.index(checkout_plan) < workflow.index(materialize)
    assert workflow.index(materialize) < workflow.index(validate)
    assert '"$PUBLICATION_MODE" = "incremental"' in validate
    assert '"$PUBLICATION_MODE" = "full"' in validate
    assert "python tools/validate.py --since" in validate
    assert f"--previous-base {previous}" in validate
    assert "python tools/validate.py\n" in validate
    assert workflow.index(validate) < workflow.index(plan)


def test_staging_expands_only_after_the_explicit_incremental_fallback_signal():
    root = pathlib.Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/publish.yml").read_text()
    stage = _workflow_step(workflow, "Stage active-only public snapshot")
    expand = _workflow_step(workflow, "Expand checkout for staging fallback")
    validate = _workflow_step(workflow, "Validate the complete staging fallback")
    complete = _workflow_step(workflow, "Stage the complete fallback")
    publish = _workflow_step(workflow, "Upload, verify, and atomically activate snapshot")
    availability = _workflow_step(workflow, "Publish availability after activation")
    retained = _workflow_step(workflow, "Keep availability after accepted delta")

    assert "--require-incremental" in stage
    assert f'{FULL_REQUIRED_EXIT}) echo "full_required=true"' in stage
    assert "steps.stage.outputs.full_required == 'true'" in expand
    assert "GITHUB_TOKEN: ${{ github.token }}" in expand
    assert "git sparse-checkout disable" in expand
    assert "python" not in expand
    assert "steps.stage.outputs.full_required == 'true'" in validate
    assert "python tools/validate.py" in validate
    assert "steps.stage.outputs.full_required == 'true'" in complete
    assert "--full" in complete
    assert workflow.index(stage) < workflow.index(expand) < workflow.index(validate)
    assert workflow.index(validate) < workflow.index(complete) < workflow.index(publish)
    assert "steps.checkout_plan.outputs.mode == 'full'" in availability
    assert "steps.stage.outputs.full_required == 'true'" in availability
    assert "steps.checkout_plan.outputs.mode == 'incremental'" in retained
    assert "steps.stage.outputs.full_required != 'true'" in retained


def _run_publication_materialization(repo, tmp_path, base, *, mode="incremental"):
    from validation_scope import scope_of, sparse_checkout_paths

    root = pathlib.Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/publish.yml").read_text()
    script = _workflow_script(
        workflow, "Materialize accepted delta or expand full checkout"
    )
    accepted = tmp_path / "accepted-paths"
    scope = scope_of(repo.path, base)
    paths = (
        sparse_checkout_paths(scope)
        if scope is not None
        else (f"registrations/results/{FIXTURE_FIRST.rsplit('-v', 1)[0]}.json",)
    )
    accepted.write_bytes(b"".join(path.encode() + b"\0" for path in paths))
    environment = os.environ.copy()
    environment.update(
        {
            "PUBLICATION_MODE": mode,
            "PUBLICATION_BASE": base,
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_TOKEN": "workflow-test-token",
        }
    )

    return subprocess.run(
        ["bash", "-c", script],
        cwd=repo.path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def _sparsify_fixture(repo):
    repo.git(
        "sparse-checkout",
        "set",
        "--no-cone",
        "/schema-v3.json",
        "/scores-v1.json",
        "/LICENSE",
        "/.palomar-launched",
    )


def _blobless_checkout(repo, tmp_path):
    remote = tmp_path / "promisor.git"
    checkout = tmp_path / "partial"
    subprocess.run(
        ["git", "clone", "--quiet", "--bare", str(repo.path), str(remote)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(remote), "config", "uploadpack.allowFilter", "true"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            "--filter=blob:none",
            "--no-checkout",
            f"file://{remote}",
            str(checkout),
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "sparse-checkout",
            "set",
            "--no-cone",
            "/schema-v3.json",
            "/scores-v1.json",
            "/LICENSE",
            "/.palomar-launched",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(checkout), "checkout", "--quiet", "HEAD"], check=True
    )
    return checkout


def test_accepted_materialization_keeps_unrelated_current_payload_sparse(repo, tmp_path):
    base = repo.commit("a served base")
    identifier = FIXTURE_SECOND_ID
    repo.add_entry(identifier, 1)
    repo.commit("accept one registration")
    historical = repo.path / f"entries/{FIXTURE_FIRST}.json"
    arriving = repo.path / f"entries/{identifier}-v1.json"

    _sparsify_fixture(repo)
    assert not historical.exists()
    assert not arriving.exists()

    result = _run_publication_materialization(repo, tmp_path, base)

    assert result.returncode == 0, result.stdout + result.stderr
    assert arriving.is_file()
    assert not historical.exists()
    assert repo.git("config", "--bool", "core.sparseCheckout").strip() == "true"


def test_blobless_second_version_fetches_only_its_predecessor_projection(
    repo, tmp_path
):
    """Execute the production hydration shell against real promised blobs."""
    identifier = FIXTURE_FIRST.rsplit("-v", 1)[0]
    result_path = f"registrations/results/{identifier}.json"
    base = repo.commit("the served first version")
    predecessor_oid = repo.git("rev-parse", f"{base}:{result_path}").strip()
    repo.add_entry(identifier, 2)
    repo.commit("accept the second version")

    checkout = _blobless_checkout(repo, tmp_path)
    runner = tmp_path / "runner"
    runner.mkdir()
    current_oid = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", f"HEAD:{result_path}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    unrelated_oid = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD:takedowns.json"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    def missing() -> set[str]:
        output = subprocess.run(
            [
                "git",
                "-C",
                str(checkout),
                "rev-list",
                "--objects",
                "--missing=print",
                "HEAD",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        return {line[1:] for line in output if line.startswith("?")}

    assert {predecessor_oid, current_oid, unrelated_oid} <= missing()

    result = _run_publication_materialization(
        types.SimpleNamespace(path=checkout), runner, base
    )

    assert result.returncode == 0, result.stdout + result.stderr
    remaining = missing()
    assert predecessor_oid not in remaining
    assert current_oid not in remaining
    assert unrelated_oid in remaining
    assert (checkout / result_path).is_file()


@pytest.mark.parametrize("withdrawal_count", [0, 12])
def test_sparse_workflow_inputs_validate_stage_and_prepare_health(
    repo, tmp_path, withdrawal_count
):
    """Exercise the production boundaries against a real blobless promisor clone."""
    if withdrawal_count:
        for serial in range(2, withdrawal_count + 1):
            repo.add_entry(f"PALOMAR-2026-07-29-{serial:06d}", 1)
        repo.write_json(
            "takedowns.json",
            {
                "schema_version": 1,
                "takedowns": [
                    {
                        "id": f"PALOMAR-2026-07-29-{serial:06d}",
                        "version": 1,
                        "taken_down_at": "2026-08-08T12:00:00Z",
                        "authorized_by_login": "avigad",
                        "authorization_issue": 101 + serial,
                        "reason": "workflow fixture",
                    }
                    for serial in range(1, withdrawal_count + 1)
                ],
            },
        )
    base = repo.commit("the served revision")
    served = tmp_path / "served"
    stage_public(repo.path, served, full=True)
    served_delta = release_delta.parse((served / "release-delta.json").read_bytes())
    (served / release_delta.BASE_PATH).write_bytes(
        release_delta.canonical_base_bytes(release_delta.base_of(served_delta))
    )
    identifier = repo.next_identifier()
    repo.add_entry(identifier, 1)
    repo.commit("the accepted revision")
    checkout = _blobless_checkout(repo, tmp_path)
    runner = tmp_path / "runner"
    runner.mkdir()
    historical = checkout / f"entries/{FIXTURE_FIRST}.json"
    arriving = checkout / f"entries/{identifier}-v1.json"
    takedowns = checkout / "takedowns.json"
    assert not historical.exists()
    assert not arriving.exists()
    assert not takedowns.exists()
    arriving_oid = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", f"HEAD:entries/{identifier}-v1.json"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    takedowns_oid = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD:takedowns.json"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    missing = subprocess.run(
        ["git", "-C", str(checkout), "rev-list", "--objects", "--missing=print", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert f"?{arriving_oid}" in missing
    assert f"?{takedowns_oid}" in missing

    result = _run_publication_materialization(
        types.SimpleNamespace(path=checkout), runner, base
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert arriving.is_file()
    assert not historical.exists()
    assert not takedowns.exists()

    root = pathlib.Path(__file__).resolve().parents[1]
    validate = subprocess.run(
        [
            sys.executable,
            str(root / "tools/validate.py"),
            "--root",
            str(checkout),
            "--since",
            base,
            "--previous-base",
            str(served / release_delta.BASE_PATH),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert validate.returncode == 0, validate.stdout + validate.stderr
    append_only = subprocess.run(
        [
            sys.executable,
            str(root / "tools/check_append_only.py"),
            "--repo",
            str(checkout),
            "--base",
            base,
            "--head",
            "HEAD",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert append_only.returncode == 0, append_only.stdout + append_only.stderr
    staged = runner / "staged"
    stage = subprocess.run(
        [
            sys.executable,
            str(root / "tools/stage_public.py"),
            "--root",
            str(checkout),
            "--output",
            str(staged),
            "--previous",
            str(served / release_delta.BASE_PATH),
            "--prior",
            str(served),
            "--require-incremental",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert stage.returncode == 0, stage.stdout + stage.stderr
    staged_delta = release_delta.parse((staged / "release-delta.json").read_bytes())
    assert not any(
        row["path"].startswith("tombstones/") for row in staged_delta["stable"]
    )
    bundled = runner / "health-entries"
    prepare = subprocess.run(
        [
            sys.executable,
            str(root / "tools/publication_evidence.py"),
            "prepare",
            "--delta",
            str(staged / "release-delta.json"),
            "--previous-base",
            str(served / release_delta.BASE_PATH),
            "--database",
            str(checkout),
            "--commit",
            repo.git("rev-parse", "HEAD").strip(),
            "--entries",
            str(checkout / "entries"),
            "--bundle-entries",
            str(bundled),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert prepare.returncode == 0, prepare.stdout + prepare.stderr
    still_missing = subprocess.run(
        ["git", "-C", str(checkout), "rev-list", "--objects", "--missing=print", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert f"?{takedowns_oid}" in still_missing
    expected = {f"{identifier}-v1.json"}
    assert {path.name for path in bundled.iterdir()} == expected


def test_complete_materialization_expands_the_current_payload_tree(repo, tmp_path):
    base = repo.commit("a served base")
    identifier = FIXTURE_SECOND_ID
    repo.add_entry(identifier, 1)
    repo.commit("a transition requiring complete checks")
    historical = repo.path / f"entries/{FIXTURE_FIRST}.json"
    arriving = repo.path / f"entries/{identifier}-v1.json"

    _sparsify_fixture(repo)
    assert not historical.exists()
    assert not arriving.exists()

    result = _run_publication_materialization(repo, tmp_path, base, mode="full")

    assert result.returncode == 0, result.stdout + result.stderr
    assert historical.is_file()
    assert arriving.is_file()
    sparse = subprocess.run(
        ["git", "-C", str(repo.path), "config", "--bool", "core.sparseCheckout"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert sparse.returncode != 0 or sparse.stdout.strip() == "false"


def test_sparse_materialization_accepts_an_explicitly_absent_first_write(
    repo, tmp_path
):
    base = repo.commit("a served base")
    repo.add_entry(FIXTURE_SECOND_ID, 1)
    repo.commit("register a new result and submission")

    result = _run_publication_materialization(repo, tmp_path, base)

    assert result.returncode == 0, result.stderr


def test_sparse_materialization_fails_closed_when_the_base_cannot_resolve(
    repo, tmp_path
):
    repo.commit("a served base")
    repo.add_entry(FIXTURE_SECOND_ID, 1)
    repo.commit("register a new result")

    result = _run_publication_materialization(repo, tmp_path, "not-a-revision")

    assert result.returncode != 0


def test_sparse_materialization_fails_closed_when_a_base_blob_cannot_be_read(
    repo, tmp_path
):
    """Execute the workflow shell, including errexit, against a missing blob."""
    base = repo.commit("a served base")
    relative = f"registrations/results/{FIXTURE_FIRST.rsplit('-v', 1)[0]}.json"
    object_id = repo.git("rev-parse", f"{base}:{relative}").strip()
    repo.add_entry(FIXTURE_FIRST.rsplit("-v", 1)[0], 2)
    repo.commit("append one version")
    loose = repo.path / ".git" / "objects" / object_id[:2] / object_id[2:]
    assert loose.is_file(), "the throwaway repository unexpectedly packed its new blob"
    loose.unlink()

    result = _run_publication_materialization(repo, tmp_path, base)

    assert result.returncode != 0


def test_delta_uses_the_triggering_sha_but_full_mode_refreshes_current_main():
    root = pathlib.Path(__file__).resolve().parents[1]
    health = (root / ".github/workflows/publish-health.yml").read_text()
    triggering = _workflow_step(health, "Check out triggering publication tooling")
    conservative = _workflow_step(
        health, "Check out current main for the conservative sweep"
    )

    assert "ref: ${{ github.event.workflow_run.head_sha || github.sha }}" in triggering
    assert "filter: tree:0" in triggering
    assert "/tools/" in triggering
    assert "path: triggering" in triggering
    assert "steps.mode.outputs.mode == 'full'" in conservative
    assert "steps.delta_check.outputs.missing == 'true'" in conservative
    assert "ref: main" in conservative
    assert "path: current" in conservative
    assert "sparse-checkout" not in conservative
    assert health.index(triggering) < health.index(conservative)
    assert health.index(conservative) < health.index(
        "      - name: Check every registered record's rendered evidence"
    )

    delta = _workflow_step(health, "Check the release delta's rendered evidence")
    whole = _workflow_step(health, "Check every registered record's rendered evidence")
    assert "working-directory: triggering" in delta
    assert "working-directory: current" in whole


def test_recovery_and_constant_probe_are_wired_to_conservative_results_only():
    root = pathlib.Path(__file__).resolve().parents[1]
    health = (root / ".github/workflows/publish-health.yml").read_text()
    mode = _workflow_step(health, "Choose proportional or conservative health")
    recovery = _workflow_step(health, "Try once to unstick it")
    report = _workflow_step(health, "Report missing publication evidence")
    front_doors = _workflow_step(
        health, "Check that no second front door serves the registry"
    )
    installing = _workflow_step(
        health, "Install publisher dependencies for the conservative audit"
    )
    audit = _workflow_step(health, "Reconcile the complete record set with R2")

    assert '--publication-event "$PUBLICATION_EVENT"' in mode
    assert '--triggering-actor "$TRIGGERING_ACTOR"' in mode
    assert "always()" in recovery
    assert "steps.full_check.outputs.missing == 'true'" in recovery
    assert "steps.delta_check.outputs.missing" not in recovery
    assert "steps.full_check.outputs.missing == 'true'" in report
    assert "steps.delta_check.outputs.missing" not in report
    assert "if: always()" in front_doors
    assert "steps.mode.outputs.mode" not in front_doors
    assert "working-directory: triggering" in front_doors
    assert "python tools/check_published.py --second-front-doors-only" in front_doors
    assert "python -m pip install" in installing
    assert "CLOUDFLARE_ACCOUNT_ID" not in installing
    assert "python -m pip install" not in audit
    assert "CLOUDFLARE_ACCOUNT_ID" in audit
    assert "working-directory: current" in audit
