"""Accepted-event validation costs what changed.

The validator reads and hashes every render and evidence bundle in the
database. At twenty-five files an entry that is most of what a validation run
does, and it ran on every push, where a push happens once per accepted result.

The exact projection transition is proved from the already validated base
commit, so neither payload nor entry-metadata reads grow with the registry.

Scoping is sound only because a published record is immutable and a schema
freezes once an entry uses it: a record that passed once passes for ever. The
whole job of `scope_of` is to refuse to scope when anything that could make
that untrue has moved, and its default branch is to refuse.

The diff is computed here rather than taken from the job that checks the
append-only invariant. Those are sibling jobs with no dependency between them
and the repository has no enforceable branch protection, so "the other job
proved the old records are untouched" is not a fact this one may assume.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest
import registration_projection
import release_delta
import validate as validate_module
import validation_scope as validation_scope_module
from entry_validation import ENTRY_SCHEMA_EVALUATION_ERROR
from stage_public import stage_public
from validate import FULL_CHECKOUT_EXIT, validate
from validation_scope import (
    ValidationScope,
    scope_of,
    sparse_checkout_paths,
    sparse_dependency_paths,
)

SECOND = "PALOMAR-2026-07-29-000002"
TRUSTED_TOOLS = (
    "validate.py",
    "validation_scope.py",
    "bundle_reference_validation.py",
    "correction_validation.py",
    "entry_validation.py",
    "schema_policy.py",
    "evidence_validation.py",
    "render_validation.py",
    "registration_projection.py",
    "score_validation.py",
    "changed_records.py",
    "release_delta.py",
    "takedowns.py",
)


def _copy_trusted_validator(destination):
    root = pathlib.Path(__file__).resolve().parents[1]
    destination.mkdir()
    for name in TRUSTED_TOOLS:
        shutil.copy(root / "tools" / name, destination / name)


def test_a_scoped_run_reads_and_hashes_only_the_registration_that_arrived(repo, monkeypatch):
    """Both payload and metadata work stay independent of registry size."""
    import evidence_validation
    import render_validation

    hashed: list[pathlib.Path] = []
    entries_opened: list[pathlib.Path] = []
    projections_opened: list[pathlib.Path] = []
    evidence_sha256 = evidence_validation._sha256
    render_sha256 = render_validation._sha256
    original_read_text = pathlib.Path.read_text
    original_read_bytes = pathlib.Path.read_bytes

    def counted(original):
        def digest(path):
            hashed.append(path)
            return original(path)

        return digest

    def counted_read_text(path, *args, **kwargs):
        if path.parent == repo.path / "entries":
            entries_opened.append(path)
        return original_read_text(path, *args, **kwargs)

    def counted_read_bytes(path, *args, **kwargs):
        if path.is_relative_to(repo.path / "registrations"):
            projections_opened.append(path)
        return original_read_bytes(path, *args, **kwargs)

    monkeypatch.setattr(evidence_validation, "_sha256", counted(evidence_sha256))
    monkeypatch.setattr(render_validation, "_sha256", counted(render_sha256))
    monkeypatch.setattr(pathlib.Path, "read_text", counted_read_text)
    monkeypatch.setattr(pathlib.Path, "read_bytes", counted_read_bytes)
    bundle_costs = {}
    metadata_reads = {}
    projection_reads = {}
    projection_writes = {}
    last_serial = 1
    for size in (2, 20):
        for serial in range(last_serial + 1, size + 1):
            repo.add_entry(f"PALOMAR-2026-07-29-{serial:06d}", 1)
        last_serial = size
        base = repo.commit(f"a database of {size}")
        last_serial += 1
        added = f"PALOMAR-2026-07-29-{last_serial:06d}"
        repo.add_entry(added, 1)
        repo.commit("one more")
        scope = scope_of(repo.path, base)
        assert scope is not None, "it refused to scope a plain addition"
        hashed.clear()
        entries_opened.clear()
        projections_opened.clear()
        assert validate(repo.path, scope) == []
        assert hashed and all(
            added in path.as_posix() for path in hashed
        ), "it reopened an unchanged historical immutable bundle"
        bundle_costs[size] = len(hashed)
        metadata_reads[size] = len(entries_opened)
        projection_reads[size] = len(projections_opened)
        projection_writes[size] = len(scope.registration_paths)
        assert metadata_reads[size] == 1
        assert all(
            added in path.name
            or path.parent.name in {"submissions", "days", "identities"}
            for path in projections_opened
        ), projections_opened
    assert bundle_costs[2] == bundle_costs[20], bundle_costs
    assert metadata_reads[2] == metadata_reads[20] == 1, metadata_reads
    assert projection_reads[2] == projection_reads[20] == 5, projection_reads
    assert projection_writes[2] == projection_writes[20] == 4, projection_writes


def test_sparse_checkout_paths_are_exactly_the_closed_accepted_delta(repo):
    base = repo.commit("before one accepted registration")
    identifier = "PALOMAR-2026-07-29-000002"
    repo.add_entry(identifier, 1)
    repo.commit("accept one registration")

    scope = scope_of(repo.path, base)
    assert scope is not None
    paths = sparse_checkout_paths(scope)

    assert paths == tuple(sorted(scope.frozen_paths | scope.registration_paths))
    assert f"entries/{identifier}-v1.json" in paths
    assert f"scores/{identifier}-v1.json" in paths
    assert f"registrations/results/{identifier}.json" in paths
    assert "registrations/days/2026-07-29.json" in paths
    assert not any(
        path.startswith("entries/PALOMAR-2026-07-29-000001") for path in paths
    )


def test_sparse_checkout_does_not_reopen_an_unchanged_withdrawal(repo):
    first = "PALOMAR-2026-07-29-000001"
    repo.write_json(
        "takedowns.json",
        {
            "schema_version": 1,
            "takedowns": [
                {
                    "id": first,
                    "version": 1,
                    "taken_down_at": "2026-08-08T12:00:00Z",
                    "authorized_by_login": "avigad",
                    "authorization_issue": 101,
                    "reason": "fixture withdrawal",
                }
            ],
        },
    )
    base = repo.commit("serve one withdrawn record")
    identifier = "PALOMAR-2026-07-29-000002"
    repo.add_entry(identifier, 1)
    repo.commit("accept an unrelated registration")

    scope = scope_of(repo.path, base)
    assert scope is not None
    paths = sparse_checkout_paths(scope)

    assert f"entries/{first}-v1.json" not in paths
    assert f"entries/{identifier}-v1.json" in paths


def test_sparse_dependencies_include_a_correction_baseline_and_reused_identity(repo):
    identifier = "PALOMAR-2026-07-29-000001"
    baseline = repo.path / f"entries/{identifier}-v1.json"
    base = repo.commit("before a metadata correction")
    corrected = repo.entry_data(identifier, 2)
    corrected["registry_correction"] = {
        "based_on": {
            "path": f"entries/{identifier}-v1.json",
            "version": 1,
            "sha256": hashlib.sha256(baseline.read_bytes()).hexdigest(),
        }
    }
    repo.install_entry(corrected)
    repo.commit("accept a metadata correction")

    scope = scope_of(repo.path, base)
    assert scope is not None
    dependencies = sparse_dependency_paths(repo.path, scope)
    result = repo.read_json(f"registrations/results/{identifier}.json")

    assert f"entries/{identifier}-v1.json" in dependencies
    assert registration_projection.identity_path(result["identity"]) in dependencies
    assert not set(dependencies) & set(sparse_checkout_paths(scope))


def test_sparse_dependency_cli_uses_nul_delimited_paths(tmp_path, repo):
    root = pathlib.Path(__file__).resolve().parents[1]
    identifier = "PALOMAR-2026-07-29-000001"
    base = repo.commit("before another version")
    repo.add_entry(identifier, 2)
    repo.commit("accept another version")
    output = tmp_path / "dependency-paths"

    result = subprocess.run(
        [
            sys.executable,
            str(root / "tools/validate.py"),
            "--root",
            str(repo.path),
            "--since",
            base,
            "--sparse-dependencies",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert output.read_bytes().endswith(b"\0")
    result_projection = repo.read_json(f"registrations/results/{identifier}.json")
    assert registration_projection.identity_path(result_projection["identity"]).encode() in (
        output.read_bytes().split(b"\0")
    )


def test_an_arriving_version_named_by_a_new_takedown_cannot_use_the_narrow_path(
    repo, tmp_path
):
    base = repo.commit("the served database revision")
    served = tmp_path.with_name(tmp_path.name + "-future-takedown-served")
    stage_public(repo.path, served, full=True)
    delta = release_delta.parse((served / "release-delta.json").read_bytes())
    base_path = served / release_delta.BASE_PATH
    base_path.write_bytes(
        release_delta.canonical_base_bytes(release_delta.base_of(delta))
    )

    identifier = "PALOMAR-2026-07-29-000002"
    repo.add_entry(identifier, 1)
    repo.write_json(
        "takedowns.json",
        {
            "schema_version": 1,
            "takedowns": [{
                "id": identifier,
                "version": 1,
                "taken_down_at": "2026-08-08T12:00:00Z",
                "authorized_by_login": "avigad",
                "authorization_issue": 102,
                "reason": "same-event fixture",
            }],
        },
    )
    repo.commit("accept and immediately withdraw one version")
    output = tmp_path / "accepted-paths"

    validate_main_result = subprocess.run(
        [
            sys.executable,
            str(pathlib.Path(__file__).resolve().parents[1] / "tools/validate.py"),
            "--root",
            str(repo.path),
            "--since",
            base,
            "--previous-base",
            str(base_path),
            "--sparse-paths",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert validate_main_result.returncode == FULL_CHECKOUT_EXIT
    assert output.read_bytes() == b""
    assert "takedowns.json" in validate_main_result.stdout


def test_sparse_path_cli_uses_nul_paths_and_requests_full_for_policy(tmp_path, repo):
    root = pathlib.Path(__file__).resolve().parents[1]
    base = repo.commit("before one accepted registration")
    identifier = "PALOMAR-2026-07-29-000002"
    repo.add_entry(identifier, 1)
    repo.commit("accept one registration")
    output = tmp_path / "accepted-paths"

    accepted = subprocess.run(
        [
            sys.executable,
            str(root / "tools/validate.py"),
            "--root",
            str(repo.path),
            "--since",
            base,
            "--sparse-paths",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert accepted.returncode == 0, accepted.stderr
    assert output.read_bytes().split(b"\0")[-1] == b""
    assert f"entries/{identifier}-v1.json".encode() in output.read_bytes().split(b"\0")

    policy_base = repo.commit("before policy moves")
    repo.write("takedowns.json", '{"schema_version": 1, "takedowns": []}\n')
    repo.commit("move policy bytes")
    complete = subprocess.run(
        [
            sys.executable,
            str(root / "tools/validate.py"),
            "--root",
            str(repo.path),
            "--since",
            policy_base,
            "--sparse-paths",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert complete.returncode == FULL_CHECKOUT_EXIT
    assert output.read_bytes() == b""


def test_sparse_cli_delegates_only_scope_planning_to_the_extracted_boundary(
    tmp_path, monkeypatch
):
    scope = ValidationScope(
        entries=frozenset({"entries/example.json"}),
        frozen_paths=frozenset({"entries/example.json"}),
        base="base-revision",
        registration_paths=frozenset({"registrations/results/example.json"}),
    )
    calls = {"classify": 0, "plan": 0}

    def classify(root, base):
        assert (root, base) == (tmp_path, "base-revision")
        calls["classify"] += 1
        return scope

    def plan(selected):
        assert selected is scope
        calls["plan"] += 1
        return ("entries/example.json", "registrations/results/example.json")

    monkeypatch.setattr(validation_scope_module, "scope_of", classify)
    monkeypatch.setattr(validation_scope_module, "sparse_checkout_paths", plan)
    output = tmp_path / "accepted-paths"

    assert validate_module.main([
        "--root",
        str(tmp_path),
        "--since",
        "base-revision",
        "--sparse-paths",
        str(output),
    ]) == 0
    assert output.read_bytes() == (
        b"entries/example.json\0registrations/results/example.json\0"
    )
    assert calls == {"classify": 1, "plan": 1}


def test_sparse_checkout_refuses_a_hostile_served_takedown_disagreement(
    tmp_path, repo
):
    root = pathlib.Path(__file__).resolve().parents[1]
    base = repo.commit("the served database revision")
    served = tmp_path.with_name(tmp_path.name + "-served")
    stage_public(repo.path, served, full=True)
    delta = release_delta.parse((served / "release-delta.json").read_bytes())
    previous = release_delta.base_of(delta)
    previous["takedowns_git_blob"] = "f" * 40
    previous_path = served / release_delta.BASE_PATH
    previous_path.write_bytes(release_delta.canonical_base_bytes(previous))
    repo.add_entry("PALOMAR-2026-07-29-000002", 1)
    repo.commit("an otherwise closed accepted registration")
    output = tmp_path / "accepted-paths"

    result = subprocess.run(
        [
            sys.executable,
            str(root / "tools/validate.py"),
            "--root",
            str(repo.path),
            "--since",
            base,
            "--previous-base",
            str(previous_path),
            "--sparse-paths",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == FULL_CHECKOUT_EXIT
    assert output.read_bytes() == b""
    assert "takedown authority disagrees" in result.stdout


def test_scoped_validation_does_not_reopen_an_unchanged_takedown_policy(
    tmp_path, repo
):
    """Pin the induction that makes the narrow path independent of W.

    This synthetic base deliberately bypasses the full main validator with a
    malformed manifest. The scoped run's premise is that main already checked
    that exact unchanged policy; changes to the manifest are independently
    pinned below as a mandatory full fallback.
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    repo.write_json("takedowns.json", {"schema_version": 1, "takedowns": [{}]})
    base = repo.commit("a malformed prior policy fixture")
    repo.add_entry("PALOMAR-2026-07-29-000002", 1)
    repo.commit("an otherwise closed registration")
    output = tmp_path.with_name(tmp_path.name + "-accepted-paths")

    planned = subprocess.run(
        [
            sys.executable,
            str(root / "tools/validate.py"),
            "--root",
            str(repo.path),
            "--since",
            base,
            "--sparse-paths",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert planned.returncode == 0, planned.stderr
    assert output.read_bytes()

    validated = subprocess.run(
        [
            sys.executable,
            str(root / "tools/validate.py"),
            "--root",
            str(repo.path),
            "--since",
            base,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert validated.returncode == 0, validated.stderr
    assert "takedowns.json" not in validated.stdout + validated.stderr


def test_projection_cost_terms_are_result_local_and_the_day_state_is_a_counter(repo):
    """Versions grow only one result doc; same-day allocation never grows a list."""
    identifier = "PALOMAR-2026-07-29-000001"
    for version in range(2, 8):
        base = repo.commit(f"before v{version}")
        day_before = repo.read_json("registrations/days/2026-07-29.json")
        repo.add_entry(identifier, version)
        repo.commit(f"register v{version}")
        scope = scope_of(repo.path, base)
        assert scope is not None
        assert scope.registration_paths == frozenset({
            f"registrations/results/{identifier}.json",
            f"registrations/submissions/{repo.entry_data(identifier, version)['submission']['submission_id']}.json",
        })
        result = repo.read_json(f"registrations/results/{identifier}.json")
        assert len(result["versions"]) == version
        assert repo.read_json("registrations/days/2026-07-29.json") == day_before

    base = repo.commit("before another result")
    repo.add_entry("PALOMAR-2026-07-29-000002", 1)
    repo.commit("allocate the next same-day serial")
    scope = scope_of(repo.path, base)
    assert scope is not None and len(scope.registration_paths) == 4
    assert repo.read_json("registrations/days/2026-07-29.json") == {
        "schema_version": 2,
        "date": "2026-07-29",
        "last_serial": 2,
    }


def test_an_unscoped_run_still_checks_everything(repo):
    repo.add_entry(SECOND, 1)
    assert validate(repo.path) == []


def test_a_scoped_run_still_finds_a_broken_new_record(repo):
    """Cheap is only worth having if it still refuses the thing it is for."""
    base = repo.commit("a starting point")
    entry = repo.entry_data(SECOND, 1)
    repo.install_entry(entry)
    page = repo.path / entry["challenge_render"]["artifact_path"] / "Challenge" / "index.html"
    page.write_text("<html>tampered</html>\n")
    repo.commit("a record whose render does not match its digest")

    scope = scope_of(repo.path, base)
    assert scope is not None
    assert validate(repo.path, scope), "a tampered bundle passed a scoped run"


def test_a_scoped_run_keeps_a_broken_at_rest_score_schema_red(repo):
    repo.write("scores-v1.json", "{\n")
    base = repo.commit("a broken score schema reaches the base")
    repo.add_entry(SECOND, 1)
    repo.commit("one otherwise valid addition")

    scope = scope_of(repo.path, base)

    assert scope is not None
    assert validate(repo.path, scope) == [
        "scores-v1.json: score schema is not valid JSON"
    ]


def test_a_scoped_run_keeps_a_broken_at_rest_entry_schema_red(repo):
    repo.write("schema-v3.json", "{\n")
    base = repo.commit("a broken entry schema reaches the base")
    repo.add_entry(SECOND, 1)
    repo.commit("one otherwise valid addition")

    scope = scope_of(repo.path, base)

    assert scope is not None
    assert validate(repo.path, scope) == [
        "schema-v3.json: entry schema is not valid JSON"
    ]


def test_a_scoped_run_keeps_an_unevaluable_at_rest_entry_schema_red(repo):
    repo.write_json(
        "schema-v3.json",
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$ref": "#",
        },
    )
    base = repo.commit("an unevaluable entry schema reaches the base")
    repo.add_entry(SECOND, 1)
    repo.commit("one otherwise valid addition")

    scope = scope_of(repo.path, base)

    assert scope is not None
    assert validate(repo.path, scope) == [
        f"schema-v3.json: {ENTRY_SCHEMA_EVALUATION_ERROR}"
    ]


def test_validation_catches_every_unpublished_commit_since_the_served_release(
    repo, tmp_path
):
    """A failed publication of A cannot let B validate only its own push."""
    repo.commit("the release being served")
    served = tmp_path / "served"
    stage_public(repo.path, served, full=True)
    delta = release_delta.parse((served / "release-delta.json").read_bytes())
    previous = served / release_delta.BASE_PATH
    previous.write_bytes(release_delta.canonical_base_bytes(release_delta.base_of(delta)))
    publication_base = release_delta.database_commit(previous)
    shutil.rmtree(served)

    entry = repo.entry_data(SECOND, 1)
    repo.install_entry(entry)
    render = repo.path / entry["challenge_render"]["artifact_path"] / "Challenge/index.html"
    render.write_text("<html>unvalidated A</html>\n", encoding="utf-8")
    repo.commit("A lands but publication fails")
    third = "PALOMAR-2026-07-29-000003"
    repo.add_entry(third, 1)
    repo.commit("B starts the next publication")

    scope = scope_of(repo.path, publication_base)
    assert scope is not None
    assert scope.entries == frozenset(
        {f"entries/{SECOND}-v1.json", f"entries/{third}-v1.json"}
    )
    errors = validate(repo.path, scope)
    assert any(SECOND in error for error in errors), errors


def test_trusted_validator_ignores_a_hostile_current_changed_records(
    repo, tmp_path
):
    """Sibling base modules win even if the PR supplies a scope shrinker."""
    trusted = tmp_path / "base-validator"
    _copy_trusted_validator(trusted)
    hostile = tmp_path / "pr-tools"
    hostile.mkdir()
    (hostile / "changed_records.py").write_text(
        'raise RuntimeError("loaded hostile PR scope code")\n', encoding="utf-8"
    )

    base = repo.commit("a starting point")
    repo.add_entry(SECOND, 1)
    repo.commit("one valid addition")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(hostile)
    result = subprocess.run(
        [
            sys.executable,
            str(trusted / "validate.py"),
            "--root",
            str(repo.path),
            "--since",
            base,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    assert "database is valid" in result.stdout


def test_trusted_validator_ignores_a_hostile_current_bundle_reference_module(
    repo, tmp_path
):
    """Cross-entry bundle policy must come from the trusted tools closure."""
    trusted = tmp_path / "base-validator"
    _copy_trusted_validator(trusted)
    hostile = tmp_path / "pr-tools"
    hostile.mkdir()
    (hostile / "bundle_reference_validation.py").write_text(
        'raise RuntimeError("loaded hostile PR bundle policy")\n', encoding="utf-8"
    )

    base = repo.commit("a starting point")
    repo.add_entry(SECOND, 1)
    repo.commit("one valid addition")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(hostile)
    result = subprocess.run(
        [
            sys.executable,
            str(trusted / "validate.py"),
            "--root",
            str(repo.path),
            "--since",
            base,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    assert "database is valid" in result.stdout


def test_trusted_validator_ignores_a_hostile_current_scope_module(repo, tmp_path):
    """The extracted shrinker must come from the same trusted tools closure."""
    trusted = tmp_path / "base-validator"
    _copy_trusted_validator(trusted)
    hostile = tmp_path / "pr-tools"
    hostile.mkdir()
    (hostile / "validation_scope.py").write_text(
        """\
class ValidationScope:
    def __init__(self, base):
        self.entries = frozenset()
        self.frozen_paths = frozenset()
        self.base = base
        self.registration_paths = frozenset()

def scope_of(_root, base):
    return ValidationScope(base)

def scoped_parent_errors(_root, _scope, _previous_base):
    return []

def sparse_checkout_paths(_scope):
    return ()
""",
        encoding="utf-8",
    )

    base = repo.commit("a starting point")
    entry_path = repo.add_entry(SECOND, 1)
    entry = json.loads(entry_path.read_text(encoding="utf-8"))
    entry["title"] = 7
    repo.write_json(f"entries/{entry_path.name}", entry)
    repo.reindex()
    repo.commit("add one invalid record")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(hostile)

    result = subprocess.run(
        [
            sys.executable,
            str(trusted / "validate.py"),
            "--root",
            str(repo.path),
            "--since",
            base,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 1
    assert "title" in result.stderr
    assert "Traceback" not in result.stdout + result.stderr


def test_trusted_base_validator_reports_a_hostile_broken_score_schema(
    repo, tmp_path
):
    trusted = tmp_path / "base-validator"
    _copy_trusted_validator(trusted)
    hostile = tmp_path / "pr-tools"
    hostile.mkdir()
    (hostile / "score_validation.py").write_text(
        'raise RuntimeError("loaded hostile PR score policy")\n', encoding="utf-8"
    )

    base = repo.git("rev-parse", "HEAD").strip()
    repo.write("scores-v1.json", '{"type": 7}\n')
    repo.commit("break the score schema")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(hostile)

    result = subprocess.run(
        [
            sys.executable,
            str(trusted / "validate.py"),
            "--root",
            str(repo.path),
            "--since",
            base,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 1
    assert result.stderr.splitlines() == [
        "scores-v1.json: score schema is not valid Draft 2020-12 JSON Schema"
    ]
    assert "Traceback" not in result.stdout + result.stderr


def test_trusted_base_validator_reports_a_hostile_broken_entry_schema(
    repo, tmp_path
):
    trusted = tmp_path / "base-validator"
    _copy_trusted_validator(trusted)
    hostile = tmp_path / "pr-tools"
    hostile.mkdir()
    (hostile / "entry_validation.py").write_text(
        'raise RuntimeError("loaded hostile PR entry policy")\n', encoding="utf-8"
    )

    base = repo.git("rev-parse", "HEAD").strip()
    repo.write("schema-v3.json", '{"type": 7}\n')
    repo.commit("break the entry schema")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(hostile)

    result = subprocess.run(
        [
            sys.executable,
            str(trusted / "validate.py"),
            "--root",
            str(repo.path),
            "--since",
            base,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 1
    assert result.stderr.splitlines() == [
        "schema-v3.json: is not valid Draft 2020-12 JSON Schema"
    ]
    assert "Traceback" not in result.stdout + result.stderr


# When it must refuse to scope.


@pytest.mark.parametrize(
    ("path", "contents", "message"),
    [
        ("tools/validate.py", "# the rules themselves moved\n", "decides what"),
        ("tools/validation_scope.py", "# the scope rules themselves moved\n", "decides what"),
        (
            "tools/bundle_reference_validation.py",
            "# the bundle reference rules themselves moved\n",
            "decides what",
        ),
        ("tools/entry_validation.py", "# the entry rules themselves moved\n", "decides what"),
        ("tools/schema_policy.py", "# shared schema policy itself moved\n", "decides what"),
        ("tools/evidence_validation.py", "# the evidence rules themselves moved\n", "decides what"),
        ("tools/render_validation.py", "# the render rules themselves moved\n", "decides what"),
        ("tools/score_validation.py", "# the score rules themselves moved\n", "decides what"),
        ("takedowns.json", '{"schema_version": 1, "takedowns": []}\n', "decides what"),
        ("schema-v3.json", "{}\n", "decides what"),
        ("scores-v1.json", "{}\n", "decides what"),
        ("README.md", "# something outside the record paths\n", "outside the record paths"),
    ],
)
def test_it_refuses_to_scope_when_the_rules_could_have_moved(
    repo, path, contents, message, capsys
):
    """Falling back is always correct and merely slow, so it is the default
    branch rather than the exception."""
    base = repo.commit("a starting point")
    repo.write(path, contents)
    repo.commit(f"change {path}")
    assert scope_of(repo.path, base) is None
    assert message in capsys.readouterr().out


def test_it_refuses_to_scope_what_it_cannot_diff(repo, tmp_path):
    assert scope_of(repo.path, "0" * 40) is None
    # Somewhere that is not a checkout at all. Not `tmp_path`, which is where
    # the fixture puts the repository.
    elsewhere = tmp_path / "not-a-checkout"
    elsewhere.mkdir()
    assert scope_of(elsewhere, "HEAD") is None


def test_uncommitted_record_work_falls_back_to_complete_validation(repo):
    """Only the committed diff attests file modes and additions.

    A local dirty tree remains validatable; it deliberately takes the complete
    path rather than pretending an unattested set is a release delta.
    """
    base = repo.commit("a starting point")
    repo.add_entry(SECOND, 1)
    assert scope_of(repo.path, base) is None
    assert validate(repo.path) == []


@pytest.mark.parametrize(
    "relative",
    [
        f"registrations/results/PALOMAR-2026-07-29-000001.json",
        "registrations/days/2026-07-29.json",
    ],
)
def test_uncommitted_mutable_projection_work_refuses_scoped_validation(
    repo, relative
):
    base = repo.commit("a valid base")
    repo.add_entry(SECOND, 1)
    repo.commit("a committed accepted transition")
    path = repo.path / relative
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    assert scope_of(repo.path, base) is None


@pytest.mark.parametrize(
    "path",
    [
        f"entries/nested/{SECOND}-v1.json",
        "entries/not-a-canonical-entry.json",
        f"scores/nested/{SECOND}-v1.json",
        "scores/not-a-canonical-score.json",
    ],
)
def test_nested_or_noncanonical_entry_and_score_paths_take_the_full_path(repo, path):
    base = repo.commit("a starting point")
    repo.write(path, "{}\n")
    repo.commit("add a path outside the canonical flat shape")

    scope = scope_of(repo.path, base)
    assert scope is None
    scoped_errors = validate(repo.path, scope)
    full_errors = validate(repo.path)
    assert scoped_errors == full_errors
    assert full_errors


def test_scoped_projection_check_rejects_an_old_row_changed_beside_an_addition(repo):
    base = repo.commit("a starting point")
    repo.add_entry(SECOND, 1)
    relative = "registrations/results/PALOMAR-2026-07-29-000001.json"
    result = repo.read_json(relative)
    result["versions"][0]["title"] = "silently rewritten historical title"
    repo.write_json(relative, result)
    repo.commit("add one while corrupting an old index row")

    scope = scope_of(repo.path, base)
    assert scope is not None
    assert any("changed projection paths" in error for error in validate(repo.path, scope))


@pytest.mark.parametrize("directory", ["renders", "evidence"])
def test_scoped_run_rejects_an_orphan_nested_bundle_added_beside_a_record(
    repo, directory
):
    base = repo.commit("a starting point")
    repo.add_entry(SECOND, 1)
    repo.write(f"{directory}/orphan/nested/not-referenced.txt", "orphan\n")
    repo.commit("add a record and an orphan")

    scope = scope_of(repo.path, base)
    assert scope is None
    assert any("not referenced" in error for error in validate(repo.path))


@pytest.mark.parametrize(
    ("directory", "noun"),
    [("renders", "render artifact"), ("evidence", "verification evidence")],
)
def test_scoped_run_reports_an_orphan_bundle_for_the_arriving_record(
    repo, directory, noun
):
    base = repo.commit("a starting point")
    repo.add_entry(SECOND, 1)
    orphan = f"{directory}/{SECOND}-v1/{'f' * 64}/stray.txt"
    repo.write(orphan, "orphan\n")
    repo.commit("add a record with an orphan bundle")

    scope = scope_of(repo.path, base)
    assert scope is not None
    assert f"{orphan}: {noun} is not referenced by any entry" in validate(
        repo.path, scope
    )


@pytest.mark.parametrize("kind", ["render", "evidence"])
def test_a_nested_addition_to_a_historical_bundle_is_revalidated(repo, kind):
    entry = repo.read_json("entries/PALOMAR-2026-07-29-000001-v1.json")
    base = repo.commit("a starting point")
    if kind == "render":
        bundle = entry["challenge_render"]["artifact_path"]
    else:
        bundle = entry["verification"]["evidence_path"]
    repo.write(f"{bundle}new/nested.txt", "not in the immutable manifest\n")
    repo.commit(f"add to historical {kind}")

    scope = scope_of(repo.path, base)
    assert scope is None
    errors = validate(repo.path)
    assert errors
    assert any(
        marker in error
        for marker in ("does not match", "content hashes", "unexpected")
        for error in errors
    )


def test_scoped_and_full_registration_date_checks_agree_for_a_new_v2(repo):
    identifier = "PALOMAR-2026-07-29-000001"
    base = repo.commit("version one")
    repo.add_entry(
        identifier,
        2,
        first_registered_on="2026-07-30",
        registered_at="2026-07-30T00:00:00Z",
    )
    repo.commit("version two with the wrong inherited date")

    scope = scope_of(repo.path, base)
    assert scope is not None
    scoped = validate(repo.path, scope)
    full = validate(repo.path)
    assert scoped and full
    assert any("was registered" in error for error in scoped)


def test_scoped_scores_report_a_missing_new_score(repo):
    base = repo.commit("a starting point")
    repo.add_entry(SECOND, 1)
    repo.remove(f"scores/{SECOND}-v1.json")
    repo.commit("add a record without its scores")

    scope = scope_of(repo.path, base)
    assert scope is not None
    scoped = validate(repo.path, scope)
    assert scoped == validate(repo.path)
    assert any(f"scores/{SECOND}-v1.json: missing" in error for error in scoped)


def test_scoped_scores_report_an_orphan_new_score(repo):
    orphan = "PALOMAR-2026-07-29-000404-v1.json"
    base = repo.commit("a starting point")
    repo.write_json(f"scores/{orphan}", {"schema_version": 1})
    repo.commit("add orphan scores")

    scope = scope_of(repo.path, base)
    assert scope is not None
    scoped = validate(repo.path, scope)
    assert scoped == validate(repo.path)
    assert any("not recorded for any registered version" in error for error in scoped)
