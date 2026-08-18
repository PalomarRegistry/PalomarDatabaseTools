"""The append-only invariant: what a commit may and may not do to published files."""

from __future__ import annotations

import pathlib
import subprocess

import pytest
from check_append_only import check, check_history, check_tree

ROOT = pathlib.Path(__file__).resolve().parents[1]


def violations(repo) -> list[str]:
    """Commit the working tree and report what it does to the previous commit."""
    base = repo.git("rev-parse", "HEAD").strip()
    head = repo.commit("change under test")
    return check(repo.path, base, head)


# Permitted.


def test_adding_a_new_work_is_permitted(repo):
    repo.add_entry("PALOMAR-2026-07-29-000002", 1)
    assert violations(repo) == []


def test_adding_a_new_version_is_permitted(repo):
    repo.add_entry("PALOMAR-2026-07-29-000001", 2)
    assert violations(repo) == []


def test_changing_documentation_and_the_day_counter_is_permitted(repo):
    repo.write("README.md", "# rewritten\n")
    day = repo.read_json("registrations/days/2026-07-29.json")
    day["last_serial"] += 1
    repo.write_json("registrations/days/2026-07-29.json", day)
    assert violations(repo) == []


def test_an_empty_change_is_permitted(repo):
    assert violations(repo) == []


# Forbidden.


def test_modifying_a_published_entry_is_forbidden(repo):
    data = repo.read_json("entries/PALOMAR-2026-07-29-000001-v1.json")
    data["title"] = "a better title"
    repo.write_json("entries/PALOMAR-2026-07-29-000001-v1.json", data)
    repo.reindex()
    errors = violations(repo)
    assert any("immutable, byte for byte" in error for error in errors)


def test_reformatting_a_published_entry_is_forbidden(repo):
    """Byte-for-byte, not JSON-equal: even a whitespace-only change is a violation."""
    path = repo.path / "entries" / "PALOMAR-2026-07-29-000001-v1.json"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    errors = violations(repo)
    assert any("immutable, byte for byte" in error for error in errors)


def test_deleting_a_published_entry_is_forbidden(repo):
    repo.remove("entries/PALOMAR-2026-07-29-000001-v1.json")
    repo.reindex()
    errors = violations(repo)
    assert any("never deleted or renamed" in error for error in errors)


def test_renaming_a_published_entry_is_forbidden(repo):
    repo.git("mv", "entries/PALOMAR-2026-07-29-000001-v1.json", "entries/PALOMAR-2026-07-29-000002-v1.json")
    repo.reindex()
    errors = violations(repo)
    assert any(
        "entries/PALOMAR-2026-07-29-000001-v1.json" in error and "never deleted or renamed" in error
        for error in errors
    )


def test_replacing_a_published_entry_with_a_new_work_is_forbidden(repo):
    """Deleting and adding in one commit is still a deletion."""
    repo.remove("entries/PALOMAR-2026-07-29-000001-v1.json")
    repo.add_entry("PALOMAR-2026-07-29-000002", 1)
    errors = violations(repo)
    assert any("never deleted or renamed" in error for error in errors)


def test_republishing_a_version_with_different_bytes_is_forbidden(repo):
    """A duplicate version can only land on the existing path, so it is a modification."""
    repo.write_json(
        "entries/PALOMAR-2026-07-29-000001-v1.json",
        repo.entry_data("PALOMAR-2026-07-29-000001", 1, abstract="a different abstract"),
    )
    repo.reindex()
    errors = violations(repo)
    assert any("immutable, byte for byte" in error for error in errors)


def test_modifying_a_published_render_is_forbidden(repo):
    entry = repo.read_json("entries/PALOMAR-2026-07-29-000001-v1.json")
    page = repo.path / entry["challenge_render"]["artifact_path"] / "Challenge" / "index.html"
    page.write_text("rewritten render\n", encoding="utf-8")
    errors = violations(repo)
    assert any("published render artifacts are immutable" in error for error in errors)


def test_deleting_a_published_render_is_forbidden(repo):
    entry = repo.read_json("entries/PALOMAR-2026-07-29-000001-v1.json")
    page = repo.path / entry["challenge_render"]["artifact_path"] / "Challenge" / "index.html"
    page.unlink()
    errors = violations(repo)
    assert any("published render artifacts are never deleted" in error for error in errors)


def test_making_a_published_render_executable_is_forbidden(repo):
    entry = repo.read_json("entries/PALOMAR-2026-07-29-000001-v1.json")
    page = repo.path / entry["challenge_render"]["artifact_path"] / "Challenge" / "index.html"
    page.chmod(0o755)
    errors = violations(repo)
    assert any("file mode of a published render artifact" in error for error in errors)


def test_modifying_published_verification_evidence_is_forbidden(repo):
    path = repo.add_entry("PALOMAR-2026-07-29-000002", 1)
    entry = repo.read_json(path.relative_to(repo.path).as_posix())
    report = repo.path / entry["verification"]["evidence_path"] / "mechanical-report.json"
    repo.commit("publish durable verification evidence")
    report.write_text("{}\n", encoding="utf-8")
    errors = violations(repo)
    assert any("published verification evidence is immutable" in error for error in errors)


def test_deleting_published_verification_evidence_is_forbidden(repo):
    path = repo.add_entry("PALOMAR-2026-07-29-000002", 1)
    entry = repo.read_json(path.relative_to(repo.path).as_posix())
    report = repo.path / entry["verification"]["evidence_path"] / "mechanical-report.json"
    repo.commit("publish durable verification evidence")
    report.unlink()
    errors = violations(repo)
    assert any("published verification evidence is never deleted" in error for error in errors)


# Entry schema.


def test_adding_the_entry_schema_after_launch_is_forbidden(repo):
    schema = repo.read_json("schema-v3.json")
    repo.remove("schema-v3.json")
    repo.commit("launch state missing its entry schema")
    repo.write_json("schema-v3.json", schema)
    errors = violations(repo)
    assert any(
        "schema-v3.json" in error and "added after launch" in error
        for error in errors
    )


def test_the_entry_schema_remains_disposable_before_launch(repo):
    schema = repo.read_json("schema-v3.json")
    repo.remove("schema-v3.json")
    repo.remove(".palomar-launched")
    repo.commit("pre-launch state without an entry schema")
    repo.write_json("schema-v3.json", schema)
    assert violations(repo) == []


def test_modifying_the_entry_schema_is_forbidden(repo):
    schema = repo.read_json("schema-v3.json")
    schema["properties"]["title"]["maxLength"] = 400
    repo.write_json("schema-v3.json", schema)
    errors = violations(repo)
    assert any("schema-v3.json" in error and "frozen" in error for error in errors)


def test_deleting_the_entry_schema_is_forbidden(repo):
    repo.remove("schema-v3.json")
    errors = violations(repo)
    assert any("schema-v3.json" in error and "frozen" in error for error in errors)


# Published files must be ordinary files.


def test_a_symlinked_entry_is_forbidden(repo):
    """Freezing a symlink would freeze the target string, not the bytes read through it."""
    repo.write_json("payload.json", repo.entry_data("PALOMAR-2026-07-29-000002", 1))
    (repo.path / "entries" / "PALOMAR-2026-07-29-000002-v1.json").symlink_to("../payload.json")
    errors = violations(repo)
    assert any("symbolic link" in error for error in errors)


def test_a_symlinked_schema_is_forbidden(repo):
    repo.remove("schema-v3.json")
    (repo.path / "schema-v3.json").symlink_to("README.md")
    errors = violations(repo)
    assert any("schema-v3.json" in error and "symbolic link" in error for error in errors)


def test_a_submodule_in_entries_is_forbidden(repo):
    oid = repo.git("rev-parse", "HEAD").strip()
    repo.git("update-index", "--add", "--cacheinfo", f"160000,{oid},entries/PALOMAR-2026-07-29-000002-v1.json")
    repo.git("commit", "--quiet", "-m", "sneak in a gitlink")
    base = repo.git("rev-parse", "HEAD~1").strip()
    errors = check(repo.path, base, "HEAD")
    assert any("submodule" in error for error in errors)


def test_making_a_published_entry_executable_is_forbidden(repo):
    (repo.path / "entries" / "PALOMAR-2026-07-29-000001-v1.json").chmod(0o755)
    errors = violations(repo)
    assert any("file mode of a published entry" in error for error in errors)


# History.


def test_history_of_a_well_behaved_repository_is_clean(repo):
    repo.add_entry("PALOMAR-2026-07-29-000002", 1)
    repo.commit("add PALOMAR-2026-07-29-000002 v1")
    repo.add_entry("PALOMAR-2026-07-29-000003", 1)
    repo.commit("add PALOMAR-2026-07-29-000003 v1")
    assert check_history(repo.path, "HEAD") == []


def test_history_reports_the_offending_commit(repo):
    repo.add_entry("PALOMAR-2026-07-29-000002", 1)
    repo.commit("add PALOMAR-2026-07-29-000002 v1")
    repo.remove("entries/PALOMAR-2026-07-29-000001-v1.json")
    repo.reindex()
    bad = repo.commit("quietly drop PALOMAR-2026-07-29-000001")
    repo.add_entry("PALOMAR-2026-07-29-000004", 1)
    repo.commit("carry on as if nothing happened")
    errors = check_history(repo.path, "HEAD")
    assert errors
    assert all(error.startswith(bad[:12]) for error in errors)
    assert all("never deleted or renamed" in error for error in errors)


def test_history_follows_first_parents_across_a_merge(repo):
    """A merge commit is checked against the branch it merges into."""
    repo.git("checkout", "--quiet", "-b", "topic")
    repo.remove("entries/PALOMAR-2026-07-29-000001-v1.json")
    repo.reindex()
    repo.commit("drop an entry on a topic branch")
    repo.git("checkout", "--quiet", "main")
    repo.git("merge", "--quiet", "--no-ff", "-m", "merge topic", "topic")
    errors = check_history(repo.path, "HEAD")
    assert any("never deleted or renamed" in error for error in errors)


def test_the_real_repository_history_is_clean():
    if not (ROOT / ".palomar-launched").is_file():
        pytest.skip("public tooling checkout, not the canonical database history")
    shallow = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--is-shallow-repository"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if shallow != "false":
        pytest.skip("shallow checkout; full history is not available")
    assert check_history(ROOT, "HEAD") == []


# Legacy evidence: captured bundles are frozen, the index that binds them appends.

def test_a_pre_launch_database_may_be_reshaped(repo):
    """The invariant is a promise, and the marker is where it starts.

    Before launch nobody has relied on a record, so a database may be emptied
    or reshaped. This is what lets the registry be reset without pretending
    the published files were never published.
    """
    repo.remove(".palomar-launched")
    repo.commit("leave the launch boundary")
    for path in (repo.path / "entries").iterdir():
        path.unlink()
    assert violations(repo) == []


def test_the_invariant_binds_again_once_the_marker_returns(repo):
    repo.remove(".palomar-launched")
    repo.commit("leave the launch boundary")
    repo.write(".palomar-launched", "relaunched\n")
    repo.commit("relaunch")
    for path in (repo.path / "entries").iterdir():
        path.unlink()
    assert any("never deleted" in error for error in violations(repo))


# What it costs to check, which is what decides whether it can keep running.


def test_checking_a_push_does_not_grow_with_the_registry(repo, monkeypatch):
    """The property that lets this stay a real check instead of a sampling one.

    Reading both trees costs O(everything) on a check that runs once per push,
    and `--history` did it once per commit, which is O(S²) over the registry's
    life. Only what changed can break an invariant about change.

    Measured as how much git output the check consumes, not how many git calls
    it makes: two whole-tree reads are two calls whatever the registry weighs,
    so counting calls would pass for exactly the shape this rules out.
    """
    import check_append_only as checker

    consumed = 0
    original = checker._git

    def measured(repo_path, *arguments):
        nonlocal consumed
        output = original(repo_path, *arguments)
        consumed += len(output)
        return output

    monkeypatch.setattr(checker, "_git", measured)
    costs = {}
    for size in (2, 20):
        for serial in range(10, 10 + size):
            repo.add_entry(f"PALOMAR-2026-07-29-{serial:06d}", 1)
        base = repo.commit(f"a registry of {size}")
        repo.add_entry(f"PALOMAR-2026-07-29-{90 + size:06d}", 1)
        head = repo.commit("one more")
        consumed = 0
        assert checker.check(repo.path, base, head) == []
        costs[size] = consumed
    assert costs[2] == costs[20], costs


def test_the_whole_first_parent_walk_is_linear_in_what_changed(repo):
    """Each step is a diff, so the walk costs what the registry has ever
    changed rather than its size once per commit."""
    repo.commit("a starting point")
    for serial in range(10, 16):
        repo.add_entry(f"PALOMAR-2026-07-29-{serial:06d}", 1)
        repo.commit(f"add {serial}")
    assert check_history(repo.path, "HEAD") == []


def test_a_frozen_path_that_was_never_an_ordinary_file_is_still_found(repo):
    """The one violation a diff cannot see.

    A symlink committed before the launch marker, or before this check existed,
    is never touched again, so no diff will ever mention it. The per-push check
    is allowed to be cheap because this sweep is not.
    """
    from check_append_only import check_tree

    assert check_tree(repo.path, repo.commit("a clean start")) == []

    # Committed without the checker ever seeing the change.
    target = repo.path / "entries" / "PALOMAR-2026-07-29-000001-v1.json"
    contents = target.read_bytes()
    target.unlink()
    (repo.path / "elsewhere.json").write_bytes(contents)
    target.symlink_to("../elsewhere.json")
    rev = repo.commit("a symlinked entry")

    problems = check_tree(repo.path, rev)
    assert any("symbolic link" in problem for problem in problems), problems


def test_the_tree_sweep_says_nothing_before_launch(repo):
    from check_append_only import check_tree

    repo.remove(".palomar-launched")
    assert check_tree(repo.path, repo.commit("before the promise")) == []


# The disarmed state, said out loud.


def test_a_run_before_launch_says_so_and_does_not_claim_the_invariant_holds(repo, capsys):
    """Green because nothing was checked, and green because nothing was wrong,
    are the same colour. Only the words tell them apart, so there have to be
    words: the marker is one file, and a typo in its name would leave every
    published record rewritable with CI reporting exactly what it reports on a
    good day.
    """
    from check_append_only import main

    repo.remove(".palomar-launched")
    base = repo.commit("before the promise")
    for path in (repo.path / "entries").iterdir():
        path.unlink()
    head = repo.commit("empty it")

    assert main(["--repo", str(repo.path), "--base", base, "--head", head]) == 0
    printed = capsys.readouterr().out
    assert "::warning::" in printed
    assert "enforcement is OFF" in printed
    assert ".palomar-launched" in printed
    assert "nothing was checked" in printed
    assert "invariant holds" not in printed


def test_the_history_walk_does_not_say_it_looked_at_nothing(repo, capsys):
    """It reads the marker again at every step, so an absent one at the tip
    says the invariant does not bind now, not that nothing was examined."""
    from check_append_only import main

    repo.remove(".palomar-launched")
    head = repo.commit("before the promise")

    assert main(["--repo", str(repo.path), "--history", head]) == 0
    printed = capsys.readouterr().out
    assert "enforcement is OFF" in printed
    assert "only transitions whose base carried the marker were checked" in printed
    assert "nothing was checked" not in printed


def test_every_mode_says_it(repo, capsys):
    """All three answer `[]` when the marker is absent, so all three say why."""
    from check_append_only import main

    repo.remove(".palomar-launched")
    base = repo.commit("before the promise")
    head = repo.commit("nothing in particular")
    for arguments in (
        ["--base", base, "--head", head],
        ["--history", head],
        ["--tree", head],
    ):
        capsys.readouterr()
        assert main(["--repo", str(repo.path), *arguments]) == 0
        assert "enforcement is OFF" in capsys.readouterr().out, arguments


def test_a_run_after_launch_says_the_invariant_holds_and_warns_about_nothing(repo, capsys):
    from check_append_only import main

    base = repo.git("rev-parse", "HEAD").strip()
    repo.add_entry("PALOMAR-2026-07-29-000002", 1)
    head = repo.commit("one more result")

    assert main(["--repo", str(repo.path), "--base", base, "--head", head]) == 0
    printed = capsys.readouterr().out
    assert "invariant holds" in printed
    assert "::warning::" not in printed
