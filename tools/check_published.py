"""Is every registered record's evidence actually being served?

A registration merges an entry and its rendered statement together, and the two
are published by a step that can fail on its own. On 2026-08-06 that step
failed: the first record in the registry was live, and the rendered statement a
reader looks for first returned 404 for an hour, with nothing reporting it.

An entry whose render is missing is not a broken link. It is a record asserting
that a proof was checked while the artifact standing for that claim is absent.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import pathlib
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Collection

from registration_projection import all_entry_summaries
from takedowns import load_takedowns

DEFAULT_SITE = "https://data.palomar-registry.org/"
# Hostnames that must answer for nothing. The registry's data has one front
# door; anything else that serves it is a second production service on a name
# the site never links, outside the rules attached to the zone, running whatever
# was last pushed to it. One of these did serve, for two days, and the thing
# that made it hard to notice is that nothing was looking: a check that only
# ever asks the front door cannot tell you the data has two.
#
# Checked by asking for a path the registry does promise. A hostname that does
# not resolve, refuses, or 404s is answering for nothing, which is the whole
# requirement. A hostname that answers is a finding on its own, whatever it
# happens to be serving.
MUST_NOT_SERVE = (
    "https://palomar-data-staging.palomar-server.workers.dev/",
    "https://palomar-data.palomar-server.workers.dev/",
)
# Enough to hide the round trips behind each other without asking the origin
# for more than it would see from a handful of readers. The check is bounded by
# latency, not by anything either side computes.
WORKERS = 12
USER_AGENT = "palomar-publish-health"
IDENTIFIER = r"PALOMAR-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}"
VERSION_RE = re.compile(rf"({IDENTIFIER})-v([1-9][0-9]*)")


def version_target(value: str) -> tuple[str, int]:
    """Parse the one spelling accepted by the explicit event-health inputs."""
    match = VERSION_RE.fullmatch(value)
    if match is None:
        raise ValueError(f"not a canonical version identifier: {value!r}")
    return match.group(1), int(match.group(2))


def paths_for_targets(
    entries: pathlib.Path,
    targets: Collection[tuple[str, int]],
    taken_down: Collection[tuple[str, int]],
) -> list[str]:
    """Read exactly the records named by a delta, not all result projections.

    The whole-registry mode below enumerates per-result projections. A release
    event already carries a validated, run-selected list of versions, so
    enumerating every projection merely to rediscover that list would make
    ``--only`` O(registry size) despite narrowing the network requests
    afterwards.
    """
    wanted: list[str] = []
    for identifier, version in targets:
        path = entries / f"{identifier}-v{version}.json"
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError(f"health evidence names {path.name}, which is not in entries/")
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or (record.get("id"), record.get("version")) != (
            identifier,
            version,
        ):
            raise ValueError(f"{path.name} does not describe the version its path names")
        if (identifier, version) in taken_down:
            continue
        render = record.get("challenge_render")
        if render:
            wanted.append(f"{render['artifact_path']}{render['entrypoint']}")
    return wanted


def published_paths(
    index: dict,
    entries: pathlib.Path,
    taken_down: Collection[tuple[str, int]],
) -> list[str]:
    """Every path the registry promises a reader, for every current entry.

    Superseded versions stay served, so this asks about all of them rather than
    only the newest: a record is immutable, and so is what backs it.

    A taken-down record is deliberately not served, so asking for it would make
    every run of this check fail and dispatch a publication that cannot fix it.
    `taken_down` is a required argument rather than an option because reading
    the canonical registration authority without it is always wrong.
    """
    wanted: list[str] = []
    for summary in index.get("entries", []):
        target = (summary["id"], summary["version"])
        if target in taken_down:
            continue
        record_path = entries / f"{summary['id']}-v{summary['version']}.json"
        if not record_path.is_file():
            raise FileNotFoundError(f"index names {record_path.name}, which is not in entries/")
        record = json.loads(record_path.read_text(encoding="utf-8"))
        render = record.get("challenge_render")
        if render:
            wanted.append(f"{render['artifact_path']}{render['entrypoint']}")
    return wanted


def suppressed_paths(
    index: dict,
    entries: pathlib.Path,
    taken_down: Collection[tuple[str, int]],
) -> list[str]:
    """Every path a withdrawn record used to promise, which must now be gone.

    Skipping withdrawn records entirely would let a publication that failed
    part-way leave one being served while this check reported success. A
    withdrawal is the one case where being served is the failure.
    """
    wanted: list[str] = []
    for summary in index.get("entries", []):
        if (summary["id"], summary["version"]) not in taken_down:
            continue
        record_path = entries / f"{summary['id']}-v{summary['version']}.json"
        if not record_path.is_file():
            continue
        record = json.loads(record_path.read_text(encoding="utf-8"))
        render = record.get("challenge_render")
        if render:
            wanted.append(f"{render['artifact_path']}{render['entrypoint']}")
    return wanted


def suppressed_paths_for_targets(
    entries: pathlib.Path,
    targets: Collection[tuple[str, int]],
) -> list[str]:
    """Every rendered path in an explicit, exhaustive withdrawal set."""
    return paths_for_targets(entries, targets, set())


ABSENT_STATUSES = frozenset({404, 410})


def still_served(site: str, paths: list[str], *, opener=None) -> list[str]:
    """The withdrawn paths that are not provably gone, which must be none.

    Only a definite absence proves a withdrawal propagated. A timeout, a TLS
    failure or a 503 says nothing at all, and counting those as absence would
    let an outage report a withdrawal as complete while the record was still
    being served.
    """
    opener = opener or urllib.request.urlopen
    problems = []
    base = site if site.endswith("/") else f"{site}/"
    for path in paths:
        url = f"{base}{path}"
        request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        try:
            with opener(request, timeout=30) as response:
                problems.append(f"{url} is still served ({response.status})")
        except urllib.error.HTTPError as error:
            if error.code not in ABSENT_STATUSES:
                problems.append(f"{url} responded {error.code}, which does not prove it is gone")
        except Exception as error:  # noqa: BLE001 - an unknown answer is not an absence
            problems.append(f"{url} could not be checked: {error}")
    return problems


def unreachable(site: str, paths: list[str], *, opener=None, workers: int = WORKERS) -> list[str]:
    """The paths that are not being served, in the order they were asked for.

    Requested concurrently, because what dominates a run of these is not the
    origin's thinking time but the round trips: connecting and negotiating TLS
    for each of them, one after another, is minutes of waiting for a check that
    is otherwise nearly free.

    The order of the answers is fixed rather than left to whichever finished
    first, so that a failure reads the same way twice and a diff of two runs
    means something.
    """
    opener = opener or urllib.request.urlopen
    base = site if site.endswith("/") else f"{site}/"

    def ask(path: str) -> str | None:
        url = f"{base}{path}"
        # GitHub Pages answers 403 to urllib's default User-Agent, so without
        # this every check reports every record missing, alarms hourly, and
        # asks for a deployment nobody needed.
        request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        try:
            with opener(request, timeout=30) as response:
                return f"{url} responded {response.status}" if response.status >= 400 else None
        except urllib.error.HTTPError as error:
            return f"{url} responded {error.code}"
        except Exception as error:  # noqa: BLE001 - any failure to serve is a failure
            return f"{url} could not be fetched: {error}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        answers = list(pool.map(ask, paths))
    return [answer for answer in answers if answer is not None]


def second_front_doors(origins: Collection[str], probe: str, *, opener=None) -> list[str]:
    """The origins that answer, which must be none.

    The opposite question from `unreachable`, and the failure is opposite too:
    here an answer is the problem. An origin that does not resolve, refuses the
    connection, or says the path is not there is doing exactly what is wanted.

    Deliberately not treating a transient failure as a finding. This runs on a
    schedule against hostnames that should not exist, so a timeout means the
    same thing as a refusal for the purpose of this check, and reporting one
    would teach whoever reads it to ignore the whole step.
    """
    opener = opener or urllib.request.urlopen
    problems = []
    for origin in origins:
        if not origin:
            continue
        base = origin if origin.endswith("/") else f"{origin}/"
        url = f"{base}{probe}"
        request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        try:
            with opener(request, timeout=30) as response:
                if response.status < 400:
                    problems.append(
                        f"{url} answered {response.status}: the registry's data has a "
                        "second front door"
                    )
        except urllib.error.HTTPError as error:
            if error.code < 400:
                problems.append(f"{url} answered {error.code}: a second front door")
        except Exception:  # noqa: BLE001 - not answering is the whole requirement
            continue
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=pathlib.Path, default=pathlib.Path("."))
    parser.add_argument(
        "--entries",
        type=pathlib.Path,
        help="directory holding the exact entry records selected for an event "
        "check; defaults to ROOT/entries",
    )
    parser.add_argument("--site", default=DEFAULT_SITE)
    parser.add_argument(
        "--second-front-doors-only",
        action="store_true",
        help="ask only whether anything other than the front door is serving the "
        "registry, and answer only that. A separate question from the one this "
        "tool otherwise asks, with the opposite failure: here an answer is the "
        "problem.",
    )
    parser.add_argument(
        "--only",
        metavar="ID-vN",
        action="append",
        default=[],
        help="check only these versions' rendered statements. Withdrawals are "
        "always checked in full, whatever this says.",
    )
    parser.add_argument(
        "--withdrawn-file",
        type=pathlib.Path,
        help="newline-delimited exhaustive withdrawn versions from a validated "
        "release delta, including an empty file for an empty set",
    )
    parser.add_argument(
        "--withdrawals-only",
        action="store_true",
        help="check only that every withdrawn render is gone. Used for a release "
        "delta with no added versions.",
    )
    args = parser.parse_args()
    entries = args.entries if args.entries is not None else args.root / "entries"

    # Its own mode, and deliberately not folded into the run below. The two are
    # opposite questions: there a silence is the failure, here an answer is, and
    # a single exit code covering both would say "the registry is not being
    # served" when what happened is that it is being served twice. It also needs
    # nothing from the database, so it does not read it.
    if args.second_front_doors_only:
        # Asked with a path the registry does promise, so an origin serving the
        # dataset answers and one that is not does not. `schema-v2.json` is
        # published, stable and tiny.
        elsewhere = second_front_doors(MUST_NOT_SERVE, "schema-v2.json")
        for line in elsewhere:
            print(f"::error::{line}", file=sys.stderr)
        print(f"checked {len(MUST_NOT_SERVE)} origin(s) that must serve nothing; "
              f"{len(elsewhere)} answered")
        return 1 if elsewhere else 0

    if args.withdrawn_file is not None and not (
        args.only or args.withdrawals_only
    ):
        parser.error("an explicit withdrawal set requires --only or --withdrawals-only")

    try:
        # A withdrawal is never narrowed. In event mode the exhaustive set is
        # taken from the validated, run-selected release delta; in every other mode the
        # canonical manifest remains the authority.
        if args.only or args.withdrawals_only:
            selected = [version_target(value) for value in args.only]
            if len(selected) != len(set(selected)):
                raise ValueError("--only contains a duplicate version")
            if args.withdrawn_file is not None:
                raw_withdrawn = args.withdrawn_file.read_text(encoding="utf-8").splitlines()
                if any(not value for value in raw_withdrawn):
                    raise ValueError("--withdrawn-file contains a blank line")
                taken_down = {version_target(value) for value in raw_withdrawn}
                if len(taken_down) != len(raw_withdrawn):
                    raise ValueError("--withdrawn-file contains a duplicate version")
            else:
                rows, errors = load_takedowns(args.root)
                if errors:
                    print("invalid takedowns.json:\n" + "\n".join(errors), file=sys.stderr)
                    return 1
                taken_down = {(row["id"], row["version"]) for row in rows}
            paths = paths_for_targets(entries, selected, taken_down)
            withdrawn = suppressed_paths_for_targets(entries, sorted(taken_down))
            if args.only and not paths and not set(selected).issubset(taken_down):
                print(
                    f"::error::--only named {len(selected)} version(s) and matched no "
                    "published path, which is not a pass",
                    file=sys.stderr,
                )
                return 1
            if args.only:
                print(f"checking {len(paths)} path(s) for {len(selected)} named version(s)")
        else:
            index = {"entries": all_entry_summaries(args.root)}
            targets = [
                (summary["id"], summary["version"])
                for summary in index.get("entries", [])
            ]
            rows, errors = load_takedowns(args.root, targets)
            if errors:
                print("invalid takedowns.json:\n" + "\n".join(errors), file=sys.stderr)
                return 1
            taken_down = {(row["id"], row["version"]) for row in rows}
            paths = published_paths(index, entries, taken_down)
            withdrawn = suppressed_paths(index, entries, taken_down)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        print(f"::error::post-publication target selection failed: {error}", file=sys.stderr)
        return 1

    served = still_served(args.site, withdrawn)
    for line in served:
        print(f"withdrawn but not provably gone: {line}", file=sys.stderr)

    if not paths:
        print("no active rendered statements selected")
    missing = unreachable(args.site, paths) if paths else []
    for line in missing:
        print(f"missing: {line}", file=sys.stderr)
    print(
        f"checked {len(paths)} rendered statements, {len(missing)} missing; "
        f"{len(withdrawn)} withdrawn, {len(served)} not provably gone"
    )
    return 1 if missing or served else 0


if __name__ == "__main__":
    raise SystemExit(main())
