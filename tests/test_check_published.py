"""The check that a record's evidence is actually being served."""

import json
import pathlib
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))

from check_published import main as check_published_main  # noqa: E402
from check_published import (  # noqa: E402
    published_paths,
    second_front_doors,
    still_served,
    suppressed_paths,
    unreachable,
)


class PublishedPathsTests(unittest.TestCase):
    class Response:
        def __init__(self, status):
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def database(self, *records, taken_down=()):
        root = pathlib.Path(tempfile.mkdtemp())
        (root / "entries").mkdir()
        entries = []
        for record in records:
            name = f"{record['id']}-v{record['version']}.json"
            (root / "entries" / name).write_text(json.dumps(record), encoding="utf-8")
            entries.append({"id": record["id"], "version": record["version"]})
            identifier = record["id"]
            result = root / "registrations" / "results" / f"{identifier}.json"
            result.parent.mkdir(parents=True, exist_ok=True)
            existing = json.loads(result.read_text()) if result.is_file() else {
                "schema_version": 1,
                "id": identifier,
                "accepted_at": identifier[8:18],
                "identity": {
                    "source_repository": "example/repository",
                    "project_path": None,
                    "comparator_config_path": "comparator.json",
                },
                "versions": [],
            }
            existing["versions"].append({
                "version": record["version"],
                "submission_id": f"fixture{record['version']:05d}",
                "registered_at": f"{identifier[8:18]}T00:00:00Z",
                "title": "fixture",
                "status": "accepted",
                "path": f"entries/{name}",
                "abstract": "fixture",
                "classification": {"arxiv": [], "msc2020": []},
            })
            result.write_text(json.dumps(existing), encoding="utf-8")
        (root / "index.json").write_text(json.dumps({"entries": entries}), encoding="utf-8")
        (root / "takedowns.json").write_text(json.dumps({
            "schema_version": 1,
            "takedowns": [
                {"id": identifier, "version": version,
                 "taken_down_at": "2026-08-07T00:00:00Z",
                 "authorized_by_login": "avigad", "authorization_issue": 101 + position,
                 "reason": "test"}
                for position, (identifier, version) in enumerate(taken_down)
            ],
        }), encoding="utf-8")
        return root

    def record(self, identifier="PALOMAR-2026-08-06-200113", version=1):
        return {
            "id": identifier,
            "version": version,
            "challenge_render": {
                "artifact_path": f"renders/{identifier}-v{version}/{'a' * 64}/",
                "entrypoint": "Challenge/index.html",
            },
        }

    def test_every_version_is_asked_about_not_only_the_newest(self):
        # Superseded records stay served: a record is immutable, and so is what
        # backs it.
        root = self.database(self.record(version=1), self.record(version=2))
        paths = published_paths(json.loads((root / "index.json").read_text()), root / "entries", set())
        self.assertEqual(len(paths), 2)
        self.assertTrue(all(path.endswith("Challenge/index.html") for path in paths))

    def test_a_taken_down_record_is_not_asked_for(self):
        """It is deliberately not served. Asking would fail every run of this
        check and dispatch a publication that cannot put it back."""
        root = self.database(
            self.record(version=1), self.record(version=2),
            taken_down=[("PALOMAR-2026-08-06-200113", 2)],
        )
        index = json.loads((root / "index.json").read_text())
        paths = published_paths(index, root / "entries", {("PALOMAR-2026-08-06-200113", 2)})
        self.assertEqual(len(paths), 1)
        self.assertIn("-v1/", paths[0])

    def test_the_takedown_manifest_is_read_from_the_database(self):
        """Hermetic on purpose: this passed once by treating a real network
        failure as proof the record was gone."""
        root = self.database(self.record(), taken_down=[("PALOMAR-2026-08-06-200113", 1)])

        def gone(*_, **__):
            raise urllib.error.HTTPError("https://example.test/a", 404, "", {}, None)

        with (
            mock.patch.object(sys, "argv", ["check_published.py", "--root", str(root)]),
            mock.patch.object(urllib.request, "urlopen", gone),
        ):
            self.assertEqual(check_published_main(), 0)

    def test_an_answer_that_is_not_an_absence_is_not_proof(self):
        """A timeout or a 503 says nothing. Counting it as absence would let an
        outage report a withdrawal as complete."""
        for opener in (
            lambda *_, **__: (_ for _ in ()).throw(TimeoutError("timed out")),
            lambda *_, **__: (_ for _ in ()).throw(
                urllib.error.HTTPError("https://example.test/a", 503, "", {}, None)),
            lambda *_, **__: self.Response(200),
        ):
            problems = still_served("https://example.test", ["a/index.html"], opener=opener)
            self.assertEqual(len(problems), 1, problems)

    def test_a_withdrawn_record_is_checked_for_being_gone(self):
        """Skipping it entirely would let a publication that failed part-way
        leave a withdrawn record served while this reported success."""
        root = self.database(
            self.record(version=1), self.record(version=2),
            taken_down=[("PALOMAR-2026-08-06-200113", 2)],
        )
        index = json.loads((root / "index.json").read_text())
        gone = suppressed_paths(index, root / "entries", {("PALOMAR-2026-08-06-200113", 2)})
        self.assertEqual(len(gone), 1)
        self.assertIn("-v2/", gone[0])

    def test_a_withdrawn_record_that_is_still_served_is_reported(self):
        problems = still_served(
            "https://example.test", ["a/index.html"],
            opener=lambda *_, **__: self.Response(200),
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("a/index.html", problems[0])

    def test_a_withdrawn_record_that_is_gone_is_not_reported(self):
        def missing(*_, **__):
            raise urllib.error.HTTPError("https://example.test/a", 404, "", {}, None)

        self.assertEqual(still_served("https://example.test", ["a/index.html"], opener=missing), [])

    def test_an_index_naming_a_record_that_is_not_there_is_an_error(self):
        root = self.database(self.record())
        (root / "entries").joinpath("PALOMAR-2026-08-06-200113-v1.json").unlink()
        with self.assertRaises(FileNotFoundError):
            published_paths(json.loads((root / "index.json").read_text()), root / "entries", set())


class ReachabilityTests(unittest.TestCase):
    class Response:
        def __init__(self, status):
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def test_a_served_render_is_not_reported(self):
        self.assertEqual(
            unreachable("https://example.test", ["a/index.html"],
                        opener=lambda *_, **__: self.Response(200)),
            [],
        )

    def test_the_request_identifies_itself(self):
        """Without a User-Agent, GitHub Pages answers 403 to every request.

        The check then reports every record missing, alarms hourly, and asks
        for a deployment nobody needed. Verified against the live site: the
        same URL is 200 to curl and to urllib with a User-Agent, and 403 to
        urllib without one.
        """
        seen = []

        def record(request, **_):
            seen.append(request)
            return self.Response(200)

        unreachable("https://example.test", ["a/index.html"], opener=record)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].get_header("User-agent"), "the request sent no User-Agent")

    def test_a_missing_render_is_reported_with_its_url(self):
        def missing(*_, **__):
            raise urllib.error.HTTPError("https://example.test/a/index.html", 404, "", {}, None)

        problems = unreachable("https://example.test", ["a/index.html"], opener=missing)
        self.assertEqual(len(problems), 1)
        self.assertIn("404", problems[0])
        self.assertIn("a/index.html", problems[0])

    def test_a_site_that_cannot_be_reached_at_all_is_reported(self):
        def broken(*_, **__):
            raise TimeoutError("timed out")

        problems = unreachable("https://example.test", ["a/index.html"], opener=broken)
        self.assertEqual(len(problems), 1)
        self.assertIn("could not be fetched", problems[0])


if __name__ == "__main__":
    unittest.main()


# What it costs to ask, which decides whether it can run per publication.


def test_the_paths_are_asked_for_concurrently(monkeypatch):
    """What dominates a run of these is round trips, not the origin's thinking
    time. Asked one after another, a hundred thousand records is hours of
    waiting for a check that is otherwise nearly free."""
    import threading

    from check_published import unreachable

    live = 0
    high_water = 0
    lock = threading.Lock()
    release = threading.Event()

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def slow(_request, timeout=None):
        nonlocal live, high_water
        with lock:
            live += 1
            high_water = max(high_water, live)
        release.wait(timeout=2)
        with lock:
            live -= 1
        return Response()

    paths = [f"renders/PALOMAR-2026-07-29-{n:06d}-v1/index.html" for n in range(8)]
    thread = threading.Thread(target=lambda: unreachable("https://x/", paths, opener=slow))
    thread.start()
    while high_water < 2 and thread.is_alive():
        pass
    release.set()
    thread.join(timeout=5)
    assert high_water > 1, "every request waited for the one before it"


def test_the_answers_keep_the_order_they_were_asked_in(monkeypatch):
    """So that a failure reads the same way twice and a diff of two runs means
    something."""
    from check_published import unreachable

    class Missing(Exception):
        pass

    def opener(request, timeout=None):
        raise Missing(request.full_url)

    paths = [f"renders/{n}.html" for n in range(20)]
    problems = unreachable("https://x/", paths, opener=opener)
    assert [problem.split()[0] for problem in problems] == [f"https://x/{path}" for path in paths]


def test_only_narrows_the_records_but_never_the_withdrawals(tmp_path, monkeypatch, capsys):
    """`--only` is what makes this affordable after every publication, and it
    is also strictly better at catching the incident it exists for. A
    withdrawal is a different question and is never narrowed."""
    import check_published

    asked: list[list[str]] = []
    monkeypatch.setattr(check_published, "unreachable", lambda site, paths, **k: asked.append(paths) or [])
    monkeypatch.setattr(check_published, "still_served", lambda site, paths, **k: [])
    index = {
        "schema_version": 3,
        "entries": [
            {"id": f"PALOMAR-2026-07-29-{n:06d}", "version": 1, "title": "t", "status": "accepted",
             "path": f"entries/PALOMAR-2026-07-29-{n:06d}-v1.json"}
            for n in (1, 2)
        ],
    }
    (tmp_path / "index.json").write_text(json.dumps(index))
    (tmp_path / "takedowns.json").write_text(json.dumps({"schema_version": 1, "takedowns": []}))
    (tmp_path / "entries").mkdir()
    for n in (1, 2):
        identifier = f"PALOMAR-2026-07-29-{n:06d}"
        (tmp_path / "entries" / f"{identifier}-v1.json").write_text(json.dumps({
            "id": identifier, "version": 1,
            "challenge_render": {
                "artifact_path": f"renders/{identifier}-v1/{'a' * 64}/",
                "entrypoint": "Challenge/index.html",
            },
        }))

    monkeypatch.setattr("sys.argv", [
        "check_published.py", "--root", str(tmp_path),
        "--only", "PALOMAR-2026-07-29-000002-v1",
    ])
    assert check_published.main() == 0
    assert asked, "nothing was asked for"
    assert all("000002" in path for path in asked[0]), asked[0]


def test_only_naming_nothing_is_a_failure_not_a_pass(tmp_path, monkeypatch):
    """A typo that quietly checks nothing is how a check stops being one."""
    import check_published

    (tmp_path / "index.json").write_text(json.dumps({"schema_version": 3, "entries": []}))
    (tmp_path / "takedowns.json").write_text(json.dumps({"schema_version": 1, "takedowns": []}))
    (tmp_path / "entries").mkdir()
    monkeypatch.setattr("sys.argv", [
        "check_published.py", "--root", str(tmp_path), "--only", "PALOMAR-2026-07-29-000404-v1",
    ])
    assert check_published.main() == 1


class SecondFrontDoors(unittest.TestCase):
    """The registry's data has one front door, and this is what says so.

    A second copy of the dataset on a hostname the site never links is not a
    staging convenience: it is a production service outside the rules attached
    to the zone, running whatever was last pushed to it, that nobody is looking
    at. One existed for two days and what made it hard to notice is that every
    check only ever asked the front door.
    """

    class Response:
        def __init__(self, status):
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def test_an_origin_that_answers_is_a_finding(self):
        problems = second_front_doors(
            ["https://palomar-data-staging.palomar-server.workers.dev/"],
            "schema-v2.json",
            opener=lambda *_, **__: self.Response(200),
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("second front door", problems[0])
        self.assertIn("schema-v2.json", problems[0])

    def test_an_origin_that_does_not_answer_is_what_is_wanted(self):
        """A refusal, a name that does not resolve, and a 404 all mean the same
        thing here: nothing is being served."""
        for opener in (
            lambda *_, **__: (_ for _ in ()).throw(OSError("name does not resolve")),
            lambda *_, **__: (_ for _ in ()).throw(TimeoutError("timed out")),
            lambda *_, **__: (_ for _ in ()).throw(
                urllib.error.HTTPError("https://x.test/schema-v2.json", 404, "", {}, None)),
            lambda *_, **__: (_ for _ in ()).throw(
                urllib.error.HTTPError("https://x.test/schema-v2.json", 522, "", {}, None)),
        ):
            self.assertEqual(
                second_front_doors(["https://x.test/"], "schema-v2.json", opener=opener), [],
            )

    def test_a_transient_failure_is_not_reported_as_one(self):
        """This runs on a schedule against hostnames that should not exist. An
        alarm that fires on a timeout is one people learn to ignore, and this
        step has to keep meaning something."""
        self.assertEqual(
            second_front_doors(
                ["https://x.test/"], "schema-v2.json",
                opener=lambda *_, **__: (_ for _ in ()).throw(TimeoutError("timed out")),
            ),
            [],
        )

    def test_checking_none_is_expressible(self):
        self.assertEqual(second_front_doors([""], "schema-v2.json"), [])
        self.assertEqual(second_front_doors([], "schema-v2.json"), [])


def test_second_front_doors_cli_mode_is_executable(monkeypatch, capsys):
    import check_published

    monkeypatch.setattr(check_published, "MUST_NOT_SERVE", ())
    monkeypatch.setattr(
        sys, "argv", ["check_published.py", "--second-front-doors-only"]
    )

    assert check_published.main() == 0
    assert (
        "checked 0 origin(s) that must serve nothing; 0 answered"
        in capsys.readouterr().out
    )


def _write_health_record(root, target):
    identifier, version = target.rsplit("-v", 1)
    (root / "entries" / f"{target}.json").write_text(json.dumps({
        "id": identifier,
        "version": int(version),
        "challenge_render": {
            "artifact_path": f"renders/{target}/{'a' * 64}/",
            "entrypoint": "Challenge/index.html",
        },
    }))


def test_delta_health_file_reads_are_independent_of_registry_size(tmp_path, monkeypatch):
    """The sparse event path must not open the whole index before narrowing.

    A network-request count alone missed the old O(S) term: it filtered only
    after reading every entry. Count the actual canonical files opened here.
    """
    import check_published

    costs = {}
    original = pathlib.Path.read_text
    monkeypatch.setattr(check_published, "unreachable", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(check_published, "still_served", lambda *_args, **_kwargs: [])
    for size in (11, 911):
        root = tmp_path / str(size)
        (root / "entries").mkdir(parents=True)
        targets = [f"PALOMAR-2026-08-08-{serial:06d}-v1" for serial in range(1, size + 1)]
        for target in targets:
            _write_health_record(root, target)
        (root / "index.json").write_text(json.dumps({
            "entries": [
                {"id": target.rsplit("-v", 1)[0], "version": 1}
                for target in targets
            ],
        }))
        withdrawals = root / "withdrawals.txt"
        withdrawals.write_text("")
        opened = []

        def counted(path, *args, _root=root, _opened=opened, **kwargs):
            if path.parent == _root / "entries" or path == _root / "index.json":
                _opened.append(path.relative_to(_root).as_posix())
            return original(path, *args, **kwargs)

        with (
            monkeypatch.context() as context,
            mock.patch.object(sys, "argv", [
                "check_published.py",
                "--root",
                str(root),
                "--only",
                targets[-1],
                "--withdrawn-file",
                str(withdrawals),
            ]),
        ):
            context.setattr(pathlib.Path, "read_text", counted)
            context.setattr(check_published, "unreachable", lambda *_args, **_kwargs: [])
            context.setattr(check_published, "still_served", lambda *_args, **_kwargs: [])
            assert check_published.main() == 0
        costs[size] = opened

    assert costs[11] == [f"entries/{'PALOMAR-2026-08-08-000011-v1'}.json"]
    assert costs[911] == [f"entries/{'PALOMAR-2026-08-08-000911-v1'}.json"]


def test_delta_health_never_narrows_the_explicit_withdrawal_set(
    tmp_path, monkeypatch
):
    import check_published

    active = "PALOMAR-2026-08-08-000001-v1"
    withdrawn = "PALOMAR-2026-08-08-000002-v1"
    (tmp_path / "entries").mkdir()
    _write_health_record(tmp_path, active)
    _write_health_record(tmp_path, withdrawn)
    withdrawal_file = tmp_path / "withdrawals.txt"
    withdrawal_file.write_text(f"{withdrawn}\n")
    asked = {"active": [], "withdrawn": []}
    monkeypatch.setattr(
        check_published,
        "unreachable",
        lambda _site, paths, **_kwargs: asked["active"].extend(paths) or [],
    )
    monkeypatch.setattr(
        check_published,
        "still_served",
        lambda _site, paths, **_kwargs: asked["withdrawn"].extend(paths) or [],
    )
    monkeypatch.setattr(sys, "argv", [
        "check_published.py",
        "--root",
        str(tmp_path),
        "--only",
        active,
        "--withdrawn-file",
        str(withdrawal_file),
    ])

    assert check_published.main() == 0
    assert len(asked["active"]) == 1 and active in asked["active"][0]
    assert len(asked["withdrawn"]) == 1 and withdrawn in asked["withdrawn"][0]
