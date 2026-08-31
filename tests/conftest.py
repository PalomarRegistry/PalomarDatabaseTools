"""Shared fixtures: throwaway Palomar databases backed by real git repositories."""

from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
# The registry starts empty, so the canonical record lives with the tests
# rather than being borrowed from whatever happens to be published.
SAMPLE_ENTRY = ROOT / "tests" / "fixtures" / "entry.json"
SAMPLE_ENTRY_DATA = json.loads(SAMPLE_ENTRY.read_text(encoding="utf-8"))
SAMPLE_RENDER = ROOT / "tests" / "fixtures" / "render"

sys.path.insert(0, str(ROOT / "tools"))


def _submission_id(identifier: str, version: int) -> str:
    """A stable twelve-character id per fixture record."""
    return hashlib.sha256(f"{identifier}-v{version}".encode()).hexdigest()[:12]


def _preservation_rows(data: dict) -> list[dict[str, str]]:
    source = data["source"]
    candidates = [(source["repository"], source["commit"])]
    candidates.extend(
        (dependency["repository"], dependency["revision"])
        for dependency in data["formalization"]["project_dependencies"]
        if "path" not in dependency
    )
    substantive = data["provenance"].get("substantive_formalization")
    if substantive:
        candidates.append((substantive["repository"], substantive["commit"]))
    unique = {}
    for repository, commit in candidates:
        unique.setdefault((repository.casefold(), commit), (repository, commit))
    return [
        {
            "source_repository": repository,
            "commit": commit,
            "fork_repository": "PalomarArchive/fixture",
            "ref": f"refs/tags/palomar/{data['id']}-v{data['version']}/{commit}",
        }
        for repository, commit in sorted(
            unique.values(), key=lambda item: (item[0].casefold(), item[1])
        )
    ]


SAMPLE_SCORES = {
    "statement_alignment": 5,
    "definition_fidelity": 5,
    "notability": 5,
    "literature": 4,
    "clarity": 4,
}


class Database:
    """A database on disk, optionally under git, that starts out valid."""

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        (path / "entries").mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / "schema-v3.json", path / "schema-v3.json")
        shutil.copy(ROOT / "schema-v4.json", path / "schema-v4.json")
        shutil.copy(
            ROOT / "tests" / "fixtures" / "synthetic-scores-schema.json",
            path / "scores-v1.json",
        )
        shutil.copy(ROOT / "tests" / "fixtures" / "database-license.txt", path / "LICENSE")
        self.write("README.md", "# throwaway\n")
        self.write_json("takedowns.json", {"schema_version": 1, "takedowns": []})
        self.write(".palomar-launched", "test launch boundary\n")
        self.add_entry("PALOMAR-2026-07-29-000001", 1)

    # Filesystem helpers.

    def write(self, relative: str, text: str) -> pathlib.Path:
        path = self.path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def read_json(self, relative: str) -> dict:
        return json.loads((self.path / relative).read_text(encoding="utf-8"))

    def write_json(self, relative: str, data: object) -> pathlib.Path:
        return self.write(relative, json.dumps(data, indent=2, sort_keys=True) + "\n")

    def summaries(self) -> list[dict]:
        from registration_projection import all_entry_summaries

        return all_entry_summaries(self.path)

    def next_identifier(self, day: str = "2026-07-29") -> str:
        path = self.path / f"registrations/days/{day}.json"
        last_serial = 0
        if path.is_file():
            last_serial = self.read_json(path.relative_to(self.path).as_posix())["last_serial"]
        return f"PALOMAR-{day}-{last_serial + 1:06d}"

    def remove(self, relative: str) -> None:
        (self.path / relative).unlink()

    # Database helpers.

    def entry_data(self, identifier: str, version: int, **overrides: object) -> dict:
        data = json.loads(SAMPLE_ENTRY.read_text(encoding="utf-8"))
        data["schema_version"] = 3
        data["id"] = identifier
        data["version"] = version
        data["title"] = f"{identifier} version {version}"
        if identifier != "PALOMAR-2026-07-29-000001":
            data["formalization"]["comparator_config_path"] = (
                f"Comparator/{identifier}/comparator.json"
            )
        data["first_registered_on"] = identifier[len("PALOMAR-") : len("PALOMAR-YYYY-MM-DD")]
        render = data["challenge_render"]
        render["artifact_path"] = (
            f"renders/{identifier}-v{version}/{render['artifact_tree_sha256']}/"
        )
        data.update(overrides)
        # The day of `registered_at` is `first_registered_on`, which is the day the
        # identifier carries, so a test that moves one of them moves all three
        # without having to know that. A test that wants them to disagree says
        # `registered_at=` and gets exactly the disagreement it asked for.
        if "registered_at" not in overrides:
            data["registered_at"] = f"{data['first_registered_on']}T00:00:00Z"
        data["submission"]["submission_id"] = _submission_id(identifier, version)
        data["verification"]["evidence_path"] = (
            f"evidence/{identifier}-v{version}/"
            f"{data['verification']['evidence_tree_sha256']}/"
        )
        return data

    def add_entry(self, identifier: str, version: int, **overrides: object) -> pathlib.Path:
        data = self.entry_data(identifier, version, **overrides)
        return self.install_entry(data)

    def install_entry(self, data: dict) -> pathlib.Path:
        """Publish arbitrary entry data together with its referenced render fixture."""
        self.install_evidence(data)
        self.install_scores(data)
        render_destination = self.path / data["challenge_render"]["artifact_path"]
        if not render_destination.exists():
            shutil.copytree(SAMPLE_RENDER, render_destination)
        path = self.write_json(f"entries/{data['id']}-v{data['version']}.json", data)
        self.reindex()
        return path

    def install_scores(self, data: dict) -> None:
        """Record the scores beside the database, where the real reviewer puts them.

        Not in the record: a record is served exactly as it was committed, so
        anything the public must not see has to be somewhere the publisher
        never looks, rather than something the publisher removes.
        """
        review = data["review"]
        self.write_json(
            f"scores/{data['id']}-v{data['version']}.json",
            {
                "schema_version": 1,
                "id": data["id"],
                "version": data["version"],
                "reviewed_at": review["reviewed_at"],
                "policy_commit": review["policy_commit"],
                "scores": dict(SAMPLE_SCORES),
            },
        )

    def install_evidence(self, data: dict) -> None:
        """Install a small, internally bound durable-verification fixture."""
        verification = data["verification"]
        preservation = {
            "archive_owner": "PalomarArchive",
            "archived_at": "2026-07-29T00:00:00Z",
            "receipt_sha256": "0" * 64,
            "repositories": _preservation_rows(data),
        }
        archive_receipt = {
            "schema_version": 1,
            "id": data["id"],
            "version": data["version"],
            "archive_owner": preservation["archive_owner"],
            "archived_at": preservation["archived_at"],
            "repositories": preservation["repositories"],
        }
        workflow_commit = "9" * 40
        report = {
            "status": "pass",
            "stage": "complete",
            "submission": {"submission_id": data["submission"]["submission_id"]},
            "source": {
                "repository": data["source"]["repository"],
                "commit": data["source"]["commit"],
            },
            "challenge": {"sha256": verification["challenge_sha256"]},
            "solution": {"sha256": verification["solution_sha256"]},
            "checked_at": verification["verified_at"],
            "workflow_url": verification["workflow_url"],
            "comparator_commit": verification["comparator_commit"],
            "lean4export_commit": verification["lean4export_commit"],
            "landrun_commit": verification["landrun_commit"],
            "nanoda_commit": verification["nanoda_commit"],
        }
        report.update({
            "schema_version": 1,
            "comparator": {"path": data["formalization"]["comparator_config_path"]},
            "formalization": {"path": data["formalization"]["formalization_metadata_path"]},
            "lakefile": {"path": data["formalization"]["lakefile_path"]},
        })
        report["challenge"]["path"] = data["formalization"]["challenge_path"]
        report["solution"]["path"] = data["formalization"]["solution_path"]
        if data["source"].get("project_path"):
            report["source"]["project_path"] = data["source"]["project_path"]
        tail = str(verification.get("workflow_url", "")).rsplit("/", 1)[-1]
        run_id = int(tail) if tail.isdigit() else verification.get("run_id", 1)
        provenance = {
            "schema_version": 1,
            "repository": "PalomarRegistry/PalomarSubmission",
            "run_id": run_id,
            "run_attempt": 1,
            "workflow_path": ".github/workflows/submission.yml",
            "workflow_commit": workflow_commit,
            "workflow_url": verification["workflow_url"],
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "success",
            "created_at": "2026-07-29T00:00:00Z",
            "updated_at": "2026-07-29T00:10:00Z",
            "jobs": [{
                "id": 1001,
                "name": "verify",
                "status": "completed",
                "conclusion": "success",
                "started_at": "2026-07-29T00:00:00Z",
                "completed_at": "2026-07-29T00:10:00Z",
            }],
        }
        review = {
            "schema_version": 3,
            "submission_id": data["submission"]["submission_id"],
            "source": {
                "repository": data["source"]["repository"],
                "commit": data["source"]["commit"],
            },
            "mechanical_report": verification["workflow_url"],
            "policy_commit": data["review"]["policy_commit"],
            "reviewed_at": data["review"]["reviewed_at"],
            "reviewer_models": data["review"]["reviewer_models"],
            "outcome": data["review"]["outcome"],
            "summary": data["abstract"],
            # No `warnings`, and no `scores`. The record beside this carries the
            # review's remarks; the archived review carries each of them once,
            # in the pass that made it, and the reviewer removes the top-level
            # repetition on the way out because comparing the two lists gave
            # back the severity ranking it had just taken off the findings. A
            # fixture that kept the field was describing a document the reviewer
            # does not write.
            "requested_changes": [],
            "checks": [],
        }
        encoded = {
            "mechanical-report.json": json.dumps(report, indent=2, sort_keys=True).encode() + b"\n",
            "workflow-run.json": json.dumps(provenance, indent=2, sort_keys=True).encode() + b"\n",
            "review.json": json.dumps(review, indent=2, sort_keys=True).encode() + b"\n",
        }
        encoded["source-archive.json"] = (
            json.dumps(archive_receipt, indent=2, sort_keys=True).encode() + b"\n"
        )
        files = [
            {"path": name, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
            for name, content in sorted(encoded.items())
        ]
        digests = {item["path"]: item["sha256"] for item in files}
        canonical = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
        tree_hash = hashlib.sha256(canonical).hexdigest()
        evidence_path = f"evidence/{data['id']}-v{data['version']}/{tree_hash}/"
        destination = self.path / evidence_path
        destination.mkdir(parents=True, exist_ok=True)
        for name, content in encoded.items():
            (destination / name).write_bytes(content)
        self.write_json(
            f"{evidence_path}evidence-manifest.json",
            {"schema_version": 1, "evidence_tree_sha256": tree_hash, "files": files},
        )
        verification.update({
            "workflow_commit": workflow_commit,
            "workflow_run_attempt": 1,
            "evidence_path": evidence_path,
            "evidence_tree_sha256": tree_hash,
            "mechanical_report_sha256": digests["mechanical-report.json"],
        })
        data["review"]["report"] = {"sha256": digests["review.json"]}
        preservation["receipt_sha256"] = digests["source-archive.json"]
        data["preservation"] = preservation

    def rename_entry(self, data: dict, destination: str) -> None:
        """Move a published entry to a non-canonical filename."""
        source = self.path / f"entries/{data['id']}-v{data['version']}.json"
        source.rename(self.path / destination)
        self.reindex()

    def copy_entry(self, data: dict, destination: str) -> None:
        """Duplicate a published entry under a second filename."""
        shutil.copy(self.path / f"entries/{data['id']}-v{data['version']}.json",
                    self.path / destination)

    def reindex(self) -> None:
        from registration_projection import documents_for_entries

        entries = []
        for path in sorted((self.path / "entries").glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            entries.append((f"entries/{path.name}", data))
        shutil.rmtree(self.path / "registrations", ignore_errors=True)
        for directory in ("results", "submissions", "days", "identities"):
            (self.path / "registrations" / directory).mkdir(parents=True, exist_ok=True)
        for relative, document in documents_for_entries(entries).items():
            self.write_json(relative, document)

    # Git helpers.

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.path), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def init(self) -> None:
        self.git("init", "--quiet", "--initial-branch=main")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")

    def commit(self, message: str) -> str:
        # Several publication tests deliberately stage a release below this
        # throwaway checkout. Those bucket/output fixtures are not canonical
        # database changes and must not be swept into the next fake database
        # commit merely because ``git add --all`` starts at the root.
        staged_roots = sorted(
            {
                path.parent.relative_to(self.path).parts[0]
                for path in self.path.rglob("release-delta.json")
                if path.parent != self.path
            }
        )
        staged_roots.extend(
            path.name
            for path in self.path.iterdir()
            if path.is_dir() and path.name.endswith("-health-entries")
        )
        release_files = []
        for path in self.path.glob("*.json"):
            try:
                candidate = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(candidate, dict) and {"database_commit", "records", "surfaces"} <= set(candidate):
                release_files.append(path.name)
        self.git(
            "add",
            "--all",
            "--",
            ".",
            *(f":(exclude){name}" for name in staged_roots),
            *(f":(exclude){name}" for name in release_files),
        )
        self.git("commit", "--quiet", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD").strip()


class Served:
    """The documents the releases published so far have left in the bucket.

    An incremental release patches what is being served, so a test that stages
    one needs something for it to patch. Folding each release's staged output
    into one directory is what publishing does, and it is also the only way to
    ask the question this design can get wrong: whether a page reached by
    patching is the page a rebuild would have written.
    """

    def __init__(self, database: Database, root: pathlib.Path) -> None:
        from stage_public import stage_public

        self._stage = stage_public
        self.database = database
        self.root = root
        self.path = root / "bucket"
        self.path.mkdir(parents=True, exist_ok=True)
        self.delta: dict | None = None
        self.releases = 0

    def fetched(self) -> pathlib.Path:
        """Exactly the documents the plan names, as the publication fetches them.

        A publication reads the parent publication base, works out a plan without bucket
        credentials, fetches what it names with them, works out what those
        documents point at without them again, fetches that, and stages.
        Handing the stager the whole bucket instead would hide a plan that
        under-names something, which would show up as a page written back with
        everything else on it gone.
        """
        import release_delta
        import stage_public

        ready = stage_public.plan(
            self.database.path.resolve(),
            None if self.delta is None else release_delta.base_of(self.delta),
        )
        prior = self.root / f"fetched{self.releases + 1}"
        prior.mkdir(parents=True)
        if ready is None:
            return prior
        def fetch(names: list[str]) -> None:
            for relative in names:
                source = self.path / relative
                if source.is_file():
                    (prior / relative).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, prior / relative)

        # Twice, and the second pass is worked out only after the first has
        # arrived: the number of a word's open postings page is in that word's
        # head, and the head is itself a document to be fetched.
        fetch(ready.prior_paths())
        fetch(ready.prior_paths(prior))
        return prior

    def publish(self, *, by_plan: bool = False, **options) -> pathlib.Path:
        """Stage the next release against what is served, and serve it."""
        import release_delta

        prior = self.fetched() if by_plan else self.path
        self.releases += 1
        site = self.root / f"release{self.releases}"
        self._stage(
            self.database.path,
            site,
            previous=(None if self.delta is None else release_delta.base_of(self.delta)),
            prior=prior,
            **options,
        )
        self.delta = release_delta.parse((site / "release-delta.json").read_bytes())
        if self.delta["parent"] is None:
            shutil.rmtree(self.path)
            shutil.copytree(site, self.path)
        else:
            shutil.copytree(site, self.path, dirs_exist_ok=True)
        # What the publisher deletes after it has written everything else. A
        # bucket that kept them would hide the one thing a retirement is for:
        # an object that is gone rather than emptied.
        for relative in self.delta["retired"]:
            (self.path / relative).unlink(missing_ok=True)
        return site

    def rebuild(self) -> pathlib.Path:
        """The same dataset, staged from nothing, for comparison."""
        site = self.root / f"rebuild{self.releases}"
        self._stage(self.database.path, site, full=True)
        return site


SURFACE_PREFIXES = (
    "browse/",
    "subjects/",
    "feeds/",
    "search/",
    "versions/",
    "registration-identities/",
    "repositories/",
)
SURFACE_PATHS = ("recent.json", "recent-renders.json", "feed.xml")


def surfaces(site: pathlib.Path) -> dict[str, bytes]:
    """Every derived page a release publishes, by path and by bytes."""
    found = {}
    for path in sorted(site.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(site).as_posix()
        if relative in SURFACE_PATHS or relative.startswith(SURFACE_PREFIXES):
            found[relative] = path.read_bytes()
    return found


@pytest.fixture
def db(tmp_path: pathlib.Path) -> Database:
    """A valid database with no git history."""
    return Database(tmp_path)


@pytest.fixture
def served(repo: Database, tmp_path: pathlib.Path) -> Served:
    """A database under git, with somewhere to publish it to."""
    return Served(repo, tmp_path / "public")


@pytest.fixture
def repo(tmp_path: pathlib.Path) -> Database:
    """A valid database whose initial state is committed as `base`."""
    database = Database(tmp_path)
    database.init()
    database.commit("initial")
    return database
