#!/usr/bin/env python3
"""Build the filtered, self-contained snapshot served by the public data Worker."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import shutil
from typing import Any

import changed_records
import build_registration_lookups
import patch_surfaces
import registration_projection
import release_delta
from build_browse import build_browse
from build_feeds import build_feeds
from build_recent import RECENT_PATH, build_recent, validate_entry_schema_for_recent
from build_search import build_search, head_paths, page_paths, patch_search
from build_subjects import build_subjects
from entry_validation import (
    EntrySchemaUnevaluable,
    ENTRY_SCHEMA_EVALUATION_ERROR,
    ENTRY_SCHEMA_NAME,
    ENTRY_SCHEMA_VERSION,
    entry_schema_violations,
    load_entry_schema,
)
from takedowns import committed_manifest_blob, load_takedowns, manifest_blob

IMMUTABLE_PREFIXES = ("entries/", "renders/", "evidence/")
STABLE_PATHS = ("feed.xml", RECENT_PATH)
UNKEYED = ("index.json", "release-delta.json")
FULL_REBUILD_INPUTS = frozenset(
    {
        "LICENSE",
        "requirements-tools.txt",
        "schema-v2.json",
        "scores-v1.json",
        "takedowns.json",
        ".github/workflows/publish.yml",
    }
)
NON_PUBLICATION_INPUTS = frozenset({
    ".gitattributes",
    ".gitignore",
    "README.md",
    "requirements-tools.in",
    "requirements-test.in",
    "requirements-test.txt",
})
NON_PUBLICATION_PREFIXES = (".github/", "docs/", "tests/", "worker/")
FULL_REQUIRED_EXIT = 3

ENTRY_PATH_RE = re.compile(
    r"entries/(PALOMAR-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6})-v([1-9][0-9]*)\.json"
)


class FullCheckoutRequired(Exception):
    """The safe staging answer needs canonical bytes outside a sparse checkout."""


# Every field a published review may carry, at each of the three levels that
# carry a field at all. `PalomarPolicy/schemas/public-review.schema.json` is the
# same statement said once more, and the reviewer checks it there when it writes
# the document; this is the copy that runs where the bytes are actually staged,
# from this repository's own checkout, so a review that reached `evidence/` by
# some other route is still judged before anyone can read it.
#
# An allowlist, and this is the whole point of the change. What was here named
# `scores` and `severity` and passed everything else, which answered the
# question "does this carry the two fields we removed" rather than "may a
# stranger read all of this". The review contract can grow a `confidence`, a
# `raw_score`, an `axis_scores` or a model's rationale inside a pass or a
# finding, and the redaction in the reviewer removes fields by name, so a name
# nobody has thought of yet would reach the public evidence bundle with nothing
# failing and nothing logged. A denylist cannot see what it was not told about;
# this refuses what it was not told about instead.
PUBLIC_REVIEW_KEYS = frozenset({
    "schema_version",
    "submission_id",
    "source",
    "mechanical_report",
    "policy_commit",
    "reviewed_at",
    "reviewer_models",
    "decision",
    "summary",
    "requested_changes",
    "passes",
})
PUBLIC_REVIEW_SOURCE_KEYS = frozenset({"repository", "commit"})
# `scores` is absent deliberately, and so is `severity` below: those are what
# the projection removes. Everything else a review pass records is a comment or
# the evidence for one, which is what the registry exists to show.
PUBLIC_REVIEW_PASS_KEYS = frozenset({
    "step",
    "verdict",
    "summary",
    "findings",
    "trust_level",
    "sources_checked",
    "codes_checked",
    "declarations_checked",
})
PUBLIC_REVIEW_FINDING_KEYS = frozenset({"evidence", "message"})


def _load_json(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_public_keys(value: Any, allowed: frozenset[str], location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"public evidence review has no object at {location}")
    unexpected = sorted(set(value) - allowed)
    if unexpected:
        raise ValueError(
            "public evidence review contains "
            + ", ".join(f"{location}.{key}" for key in unexpected)
            + ", which no published review may carry"
        )
    return value


def _assert_public_leaf(value: Any, location: str, *, inside_list: bool = False) -> None:
    """Below the three named levels a review is text, and flat lists of text.

    A field nobody named is one way something private arrives in public. A new
    shape inside a field that was already named is the other: `sources_checked`
    as a list of objects, each with the score the pass gave that source,
    satisfies every key check above while carrying exactly what those checks
    exist to hold back. So does a number where a sentence was.

    `null` because a pass may record no trust level. Integers are not allowed
    here at all; `schema_version` is the review's one number and is checked
    where it is read, not through this walk.
    """
    if isinstance(value, list):
        if inside_list:
            raise ValueError(f"public evidence review nests an array at {location}")
        for position, child in enumerate(value):
            _assert_public_leaf(child, f"{location}[{position}]", inside_list=True)
        return
    if isinstance(value, dict):
        raise ValueError(
            f"public evidence review contains an unexpected object at {location}; "
            "below a pass and a finding a published review carries text"
        )
    if value is not None and not isinstance(value, str):
        raise ValueError(
            f"public evidence review carries {type(value).__name__} at {location}; "
            "below a pass and a finding a published review carries text"
        )


def _assert_redacted_review(path: pathlib.Path) -> None:
    """Refuse to publish an evidence review carrying anything but the review."""
    review = _assert_public_keys(_load_json(path), PUBLIC_REVIEW_KEYS, "review")
    if not isinstance(review.get("schema_version"), int) or isinstance(
        review.get("schema_version"), bool
    ):
        raise ValueError("public evidence review declares no schema version")
    for key, value in review.items():
        if key not in ("source", "passes", "schema_version"):
            _assert_public_leaf(value, f"review.{key}")
    if "source" in review:
        source = _assert_public_keys(review["source"], PUBLIC_REVIEW_SOURCE_KEYS, "review.source")
        for key, value in source.items():
            _assert_public_leaf(value, f"review.source.{key}")
    passes = review.get("passes")
    if not isinstance(passes, list):
        raise ValueError("public evidence review has no passes array")
    for position, step in enumerate(passes):
        where = f"review.passes[{position}]"
        step = _assert_public_keys(step, PUBLIC_REVIEW_PASS_KEYS, where)
        for key, value in step.items():
            if key != "findings":
                _assert_public_leaf(value, f"{where}.{key}")
        findings = step.get("findings", [])
        if not isinstance(findings, list):
            raise ValueError(f"{where}.findings is not an array")
        for index, finding in enumerate(findings):
            place = f"{where}.findings[{index}]"
            finding = _assert_public_keys(finding, PUBLIC_REVIEW_FINDING_KEYS, place)
            for key, value in finding.items():
                _assert_public_leaf(value, f"{place}.{key}")


def _copy_relative(root: pathlib.Path, output: pathlib.Path, relative: str) -> None:
    source = root / relative
    target = output / relative
    if source.is_symlink():
        raise ValueError(f"refusing to publish symbolic path: {relative}")
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True)
    elif source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    else:
        raise ValueError(f"public snapshot source is missing: {relative}")


def _row(output: pathlib.Path, relative: str) -> dict[str, Any]:
    data = (output / relative).read_bytes()
    return {
        "path": relative,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _walk(output: pathlib.Path) -> tuple[list[dict], list[dict], list[dict]]:
    """Classify everything staged. Only a full rebuild pays for this."""
    immutable: list[dict[str, Any]] = []
    stable: list[dict[str, Any]] = []
    aggregates: list[dict[str, Any]] = []
    for path in sorted(output.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(output).as_posix()
        # Two staged files are deliberately not part of a release. The delta
        # cannot describe itself. `index.json` is the scheduled whole-tree
        # reconciliation's explicit active-set intermediate; no public surface
        # reads it, and publishing a document whose size is the registry would
        # restore an O(S) event-path term.
        if relative in UNKEYED:
            continue
        row = _row(output, relative)
        if relative.startswith(IMMUTABLE_PREFIXES):
            immutable.append(row)
        elif relative in STABLE_PATHS or relative.startswith(
            (
                "browse/",
                "feeds/",
                "registration-identities/",
                "repositories/",
                "search/",
                "subjects/",
                "tombstones/",
                "versions/",
            )
        ):
            stable.append(row)
        else:
            aggregates.append(row)
    return immutable, stable, aggregates


def _database_commit(root: pathlib.Path) -> str:
    """Which commit this release was built from, so the next one can diff it."""
    try:
        return changed_records._git(root, "rev-parse", "HEAD").strip()
    except changed_records.CannotTell:
        return "0" * 40


def _summary(entry: dict[str, Any], relative: str) -> dict[str, Any]:
    """The public summary for one new immutable record.

    The result projection carries the same fields for historical versions;
    deriving the arriving row from its immutable entry also supplies the full
    current record to recent and search without opening anything unrelated.
    """
    return {
        "id": entry.get("id"),
        "version": entry.get("version"),
        "title": entry.get("title"),
        "status": entry.get("status"),
        "path": relative,
    }


def _versions_of(
    root: pathlib.Path, identifier: str
) -> tuple[dict[str, Any], list[tuple[dict[str, Any], dict[str, Any]]]]:
    """Every version of one result from its bounded registration projection."""
    result = registration_projection.load_result(root, identifier)
    accepted_at = str(result["accepted_at"])
    versions = [
        (
            registration_projection.public_summary(row, identifier),
            registration_projection.entry_view(row, identifier, accepted_at),
        )
        for row in result["versions"]
    ]
    return dict(result["identity"]), versions


def _write_version_indexes(
    output: pathlib.Path,
    by_id: dict[str, list[dict[str, Any]]],
) -> None:
    """One document per identifier, naming every version of it being served.

    An entry page needs the versions of one result. Reading `index.json` to
    find them means fetching the whole registry to render one page: at a
    hundred thousand records that is tens of megabytes for four hundred bytes
    of answer, and it grows with every result anyone else publishes.

    Written at a stable key, so it changes only when that identifier gains or
    loses a version. Nothing combines it with another document -- the entry
    page reads this and the record it names, and both are about one result --
    so it carries no generation and needs none.

    A result whose every version has been withdrawn keeps a document with
    nothing in it, rather than losing one. That is the answer its tombstones
    already give, and it is the only answer a release that writes the documents
    it touched can agree on with a release that writes all of them.
    """
    for identifier, summaries in sorted(by_id.items()):
        if len(summaries) > registration_projection.MAX_VERSIONS_PER_RESULT:
            # Truncating would drop history from the one surface that exists to
            # show it, so this is refused rather than trimmed. Nothing should
            # reach it; if something does, that is the thing to look at.
            raise ValueError(
                f"{identifier} has {len(summaries)} active versions, more than the "
                f"{registration_projection.MAX_VERSIONS_PER_RESULT} one document may carry"
            )
        document = {
            "schema_version": 1,
            "id": identifier,
            "entries": sorted(summaries, key=lambda row: int(row["version"])),
        }
        target = output / "versions" / f"{identifier}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _copy_record(root: pathlib.Path, output: pathlib.Path, summary: dict[str, Any], entry: dict[str, Any]) -> None:
    """One record and the bundles it names, copied and never re-serialised.

    A record is published exactly as it was committed, so its bytes are a
    function of the commit and not of this file -- which is what lets them be
    cached, and lets a reader compare the two byte for byte. Anything private
    stays out of the record in the first place; `scores/` is not staged because
    nothing here reads it.
    """
    target = output / summary["path"]
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / summary["path"], target)
    render = entry.get("challenge_render")
    verification = entry.get("verification")
    if isinstance(render, dict) and isinstance(render.get("artifact_path"), str):
        _copy_relative(root, output, render["artifact_path"])
    if isinstance(verification, dict) and isinstance(verification.get("evidence_path"), str):
        _assert_redacted_review(root / verification["evidence_path"] / "review.json")
        _copy_relative(root, output, verification["evidence_path"])


def _stage_schema(root: pathlib.Path, output: pathlib.Path) -> None:
    """One schema, because there is one shape.

    It is both the contract the reviewer builds a record against and the schema
    of the data served, and those being the same document is the point: while
    they were two, a record could satisfy the contract and fail what was served
    beside it.
    """
    schema = root / ENTRY_SCHEMA_NAME
    if schema.is_symlink() or not schema.is_file():
        raise ValueError(f"{ENTRY_SCHEMA_NAME} is missing or symbolic")
    shutil.copy2(schema, output / ENTRY_SCHEMA_NAME)


def _check_against_schema(output: pathlib.Path, staged: list[dict[str, Any]]) -> None:
    """Check the records this release stages against the schema it publishes.

    A snapshot that fails its own schema is worse than one with no schema at
    all: it invites a reader to verify, and then tells them the registry is
    broken. With the projection gone this is also what keeps private material
    out of a published record: `review` is `additionalProperties: false`, so a
    record carrying its scores fails here and is never published.

    Only what this release stages. A record is copied byte for byte and frozen
    once published, and so is the schema it declares, so one that satisfied its
    schema when it was staged satisfies it for ever. Compiled once per schema
    rather than once per record: this loop used to re-read and re-compile a
    twenty-three kilobyte document for every entry in the registry.
    """
    validator, schema_errors = load_entry_schema(output)
    if schema_errors:
        raise ValueError("; ".join(schema_errors))
    if validator is None:
        raise ValueError(f"{ENTRY_SCHEMA_NAME}: loader returned no validator")
    validate_entry_schema_for_recent(validator.schema, ENTRY_SCHEMA_NAME)
    for summary in staged:
        record = _load_json(output / summary["path"])
        version = record.get("schema_version")
        if type(version) is not int or version != ENTRY_SCHEMA_VERSION:
            raise ValueError(
                f"published {summary['path']} must declare schema_version "
                f"{ENTRY_SCHEMA_VERSION}; {ENTRY_SCHEMA_NAME} is the sole entry contract"
            )
        try:
            errors = entry_schema_violations(validator, record)
        except EntrySchemaUnevaluable:
            raise ValueError(ENTRY_SCHEMA_EVALUATION_ERROR)
        if errors:
            detail = "; ".join(
                f"{'/'.join(map(str, error.path)) or '(root)'}: {error.message}"
                for error in errors[:3]
            )
            raise ValueError(f"published {summary['path']} fails {ENTRY_SCHEMA_NAME}: {detail}")


def _write_tombstones(output: pathlib.Path, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        tombstone = output / "tombstones" / f"{row['id']}-v{row['version']}.json"
        tombstone.parent.mkdir(parents=True, exist_ok=True)
        tombstone.write_text(
            json.dumps(
                {
                    "id": row["id"],
                    "version": row["version"],
                    "taken_down_on": row["taken_down_at"][:10],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )


def _delta(
    output: pathlib.Path,
    root: pathlib.Path,
    *,
    parent: str | None,
    previous: dict[str, Any] | None,
    withdrawals: list[dict[str, Any]],
    retired: list[str],
    count: int | None,
) -> list[pathlib.Path]:
    """Describe what this release wrote, and write it last.

    `stable` names only the objects this release actually staged. It used to
    name every stable object in the dataset, which is a row per active version
    and per browse page and per code, written on a publication that happens
    once per accepted result: the same O(S²) the delta exists to remove, in the
    document that exists to remove it. What that row list was doing instead --
    telling the publisher which objects to rewrite, telling `--audit` what
    should be there, and healing a surface introduced after records already
    existed -- is now the publisher rewriting what the release wrote, the
    `surfaces` number, and `whole-database-sweep.yml` respectively.
    """
    immutable, stable, aggregates = _walk(output)
    if parent is None:
        root_digest = release_delta.root_of(immutable)
        withdrawals = []
    else:
        assert previous is not None
        root_digest = release_delta.root_after(
            previous["records"]["root"], immutable, withdrawals
        )
    if count is None:
        count = len([row for row in immutable if ENTRY_PATH_RE.fullmatch(row["path"])])
    delta = {
        "schema_version": release_delta.DELTA_SCHEMA,
        "surfaces": release_delta.SURFACES,
        "parent": parent,
        "database_commit": _database_commit(root),
        "additions": sorted(immutable, key=lambda row: row["path"]),
        "withdrawals": sorted(withdrawals, key=lambda row: row["path"]),
        # Derived objects this release takes away rather than rewrites. A page
        # a withdrawal emptied stays, holding nothing, because that is what a
        # rebuild writes too; a postings head is not like that, because it
        # exists only while some result carries its word.
        "retired": sorted(retired),
        "stable": sorted(stable, key=lambda row: row["path"]),
        "aggregates": sorted(aggregates, key=lambda row: row["path"]),
        # Constant-size identity of the committed authority. A scoped accepted
        # release compares this with both the served parent and its parent Git
        # tree; it never opens or repeats the growing declaration array.
        "takedowns_git_blob": (
            committed_manifest_blob(root, "HEAD")
            if (root / ".git").exists()
            else manifest_blob(root)
        ),
        "records": {"count": count, "root": root_digest},
    }
    (output / release_delta.DELTA_PATH).write_bytes(release_delta.canonical_bytes(delta))
    return [
        output / row["path"]
        for row in delta["additions"] + delta["stable"] + delta["aggregates"]
    ]


class Plan:
    """What one incremental release changes, worked out without credentials.

    From the git diff, the arriving records it names, and the constant-size Git
    identity of the unchanged takedown authority. That is the whole point of
    the split: the step that decides what a release does needs no access to the
    bucket, and the step that has access does nothing but fetch the documents
    it is handed the names of.
    """

    def __init__(
        self,
        parent: str,
        touched: list[patch_surfaces.Touched],
        staging: list[tuple[dict[str, Any], dict[str, Any]]],
        arrived: frozenset[tuple[str, int]],
        added: int,
        registrations: list[tuple[str, dict[str, Any]]],
    ) -> None:
        self.parent = parent
        self.touched = touched
        self.staging = staging
        self.arrived = arrived
        self.added = added
        self.registrations = registrations
        self._versions_bound = False

    def prior_paths(self, prior: pathlib.Path | None = None) -> list[str]:
        """The published documents this release reads, that it does not have yet.

        Named twice, because one of them cannot be named until another has
        been read. The pages, the years and the heads of the browse and
        subject collections all follow from a result's identifier and its
        codes, but the number of a word's open postings page follows from how
        many postings that word already has, and the only document that says
        so is the word's head. So the first pass names the heads, the fetch
        step brings them back, and the second pass reads them and names the
        pages. Asking again with everything already fetched names nothing,
        which is what makes running it twice rather than once safe.
        """
        wanted = set(patch_surfaces.prior_paths(self.touched))
        wanted |= {f"versions/{item.id}.json" for item in self.touched}
        wanted |= set(head_paths(self.staging))
        for _identifier, identity in self.registrations:
            wanted.add(
                build_registration_lookups.repository_path(
                    str(identity["source_repository"])
                )
            )
            wanted.add(build_registration_lookups.identity_path(identity))
        if prior is not None:
            wanted |= set(page_paths(prior, self.staging, []))
            wanted -= {relative for relative in wanted if (prior / relative).is_file()}
        return sorted(wanted)

    def bind_prior_versions(self, prior: pathlib.Path) -> None:
        """Select active historical rows from each exact served version index.

        A growing takedown manifest used to be opened to subtract withdrawn
        rows.  The already-served per-result version document is the bounded
        projection of exactly that fact.  It is authenticated by the parent
        release and fetched only for a touched result; malformed, missing, or
        projection-disagreeing bytes request the ordinary full fallback.
        """
        for item in self.touched:
            historical = {
                int(summary["version"]): (summary, entry)
                for summary, entry in item.every
                if (item.id, int(summary["version"])) not in self.arrived
            }
            path = prior / "versions" / f"{item.id}.json"
            if path.is_symlink():
                raise patch_surfaces.Rebuild(
                    f"the release being served has a symbolic versions/{item.id}.json"
                )
            if not path.exists():
                if historical:
                    raise patch_surfaces.Rebuild(
                        f"the release being served is missing versions/{item.id}.json"
                    )
                rows: object = []
            else:
                try:
                    document = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise patch_surfaces.Rebuild(
                        f"the release being served has an invalid versions/{item.id}.json: "
                        f"{error}"
                    ) from error
                if (
                    not isinstance(document, dict)
                    or set(document) != {"schema_version", "id", "entries"}
                    or type(document.get("schema_version")) is not int
                    or document.get("schema_version") != 1
                    or document.get("id") != item.id
                    or not isinstance(document.get("entries"), list)
                ):
                    raise patch_surfaces.Rebuild(
                        f"the release being served has an invalid versions/{item.id}.json"
                    )
                rows = document["entries"]

            active: list[tuple[dict[str, Any], dict[str, Any]]] = []
            previous_version = 0
            for position, row in enumerate(rows):
                if not isinstance(row, dict) or set(row) != patch_surfaces.INDEX_ROW_KEYS:
                    raise patch_surfaces.Rebuild(
                        f"the release being served has a malformed "
                        f"versions/{item.id}.json entries[{position}]"
                    )
                version = row.get("version")
                if (
                    type(version) is not int
                    or version <= previous_version
                    or version not in historical
                    or row != historical[version][0]
                ):
                    raise patch_surfaces.Rebuild(
                        f"the release being served has a projection-disagreeing "
                        f"versions/{item.id}.json entries[{position}]"
                    )
                previous_version = version
                active.append(historical[version])
            active.extend(
                pair
                for pair in item.every
                if (item.id, int(pair[0]["version"])) in self.arrived
            )
            item.active = active
        self._versions_bound = True

    def newly_active_registrations(self) -> list[tuple[str, dict[str, Any]]]:
        """Registrations with no served version before this accepted event.

        Usually this is exactly a version-one registration. A later version can
        also reactivate a registration whose preceding versions were all taken
        down, and must restore the same lookup rows a full rebuild would write.
        """
        if not self._versions_bound:
            raise patch_surfaces.Rebuild(
                "registration activity was used before prior versions were bound"
            )
        additions: list[tuple[str, dict[str, Any]]] = []
        for identifier, identity in self.registrations:
            item = next(
                candidate for candidate in self.touched if candidate.id == identifier
            )
            historical_active = any(
                (identifier, int(summary["version"])) not in self.arrived
                for summary, _entry in item.active
            )
            if item.active and not historical_active:
                additions.append((identifier, identity))
        return additions


def plan(root: pathlib.Path, previous: dict[str, Any] | None, full: bool = False) -> Plan | None:
    """What this release adds and takes away, or None meaning "rebuild it all".

    Falling back is always correct and merely slow, so every uncertainty falls
    back: no parent, a parent built in another surface layout, an unknown
    parent commit, a dirty tree, anything under the record paths that is not an
    addition, an invalid takedown manifest.

    A takedown, policy change, or any uncertain/global change takes the full
    path. The accepted-event path is deliberately narrower: immutable record
    additions plus their exact segmented registration projections.
    """
    if full or previous is None:
        return None
    if previous.get("surfaces") != release_delta.SURFACES:
        # The parent release was built in a different layout of the derived
        # surfaces, so the objects it wrote are not the objects this one
        # writes. An incremental release never lists the bucket, so nothing
        # would notice the ones the new layout no longer writes and they would
        # stay there, served and stale. A full rebuild does list it, and takes
        # them away.
        print("staging everything: the parent release was built in an older surface layout")
        return None
    try:
        changed_paths = changed_records.committed_changes_since(
            root, previous["database_commit"]
        )
        added = changed_records.ordinary_additions_since(
            root, previous["database_commit"]
        )
        immutable_added = changed_records.ordinary_additions_since(
            root,
            previous["database_commit"],
            ("entries", "renders", "evidence", "scores"),
        )
    except changed_records.CannotTell as reason:
        print(f"staging everything: {reason}")
        return None

    projection_changes = {
        path for path in changed_paths if path.startswith("registrations/")
    }
    ignored_changes = {
        path
        for path in changed_paths
        if path in NON_PUBLICATION_INPUTS
        or path.startswith(NON_PUBLICATION_PREFIXES)
    }
    rebuild_inputs = {
        path
        for path in changed_paths
        if path in FULL_REBUILD_INPUTS
        or path.startswith("tools/")
        or path.startswith(".github/workflows/publish-")
    }
    rebuild_inputs |= (
        changed_paths
        - immutable_added
        - projection_changes
        - ignored_changes
    )
    arrived = {
        (match.group(1), int(match.group(2)))
        for path in added
        if (match := ENTRY_PATH_RE.fullmatch(path))
    }
    arriving_stems = {f"{identifier}-v{version}" for identifier, version in arrived}
    for directory in ("renders", "evidence"):
        for bundle_root in changed_records.changed_bundle_roots(
            immutable_added, directory
        ):
            parts = bundle_root.split("/")
            if len(parts) < 2 or parts[1] not in arriving_stems:
                rebuild_inputs.add(bundle_root)
    if rebuild_inputs or any(
        registration_projection.RESULT_PATH_RE.fullmatch(path) is None
        and registration_projection.SUBMISSION_PATH_RE.fullmatch(path) is None
        and registration_projection.DAY_PATH_RE.fullmatch(path) is None
        and registration_projection.IDENTITY_PATH_RE.fullmatch(path) is None
        for path in projection_changes
    ):
        outside = sorted(rebuild_inputs) or sorted(
            path
            for path in projection_changes
            if registration_projection.RESULT_PATH_RE.fullmatch(path) is None
            and registration_projection.SUBMISSION_PATH_RE.fullmatch(path) is None
            and registration_projection.DAY_PATH_RE.fullmatch(path) is None
            and registration_projection.IDENTITY_PATH_RE.fullmatch(path) is None
        )
        print(
            "staging everything: the change is not a closed accepted registration delta"
            + (f" ({outside[0]})" if outside else "")
        )
        return None

    try:
        current_takedowns = committed_manifest_blob(root, "HEAD")
        parent_takedowns = committed_manifest_blob(root, previous["database_commit"])
    except ValueError as error:
        print(f"staging everything: {error}")
        return None
    if (
        current_takedowns != parent_takedowns
        or current_takedowns != previous["takedowns_git_blob"]
    ):
        print(
            "staging everything: the committed takedown authority disagrees "
            "with the served release base"
        )
        return None
    changed = {
        match.group(1)
        for path in added
        if (match := ENTRY_PATH_RE.fullmatch(path))
    }
    arrived_entries: dict[tuple[str, int], tuple[str, dict[str, Any]]] = {}
    for target in sorted(arrived):
        relative = f"entries/{target[0]}-v{target[1]}.json"
        entry = _load_json(root / relative)
        if not isinstance(entry, dict) or (entry.get("id"), entry.get("version")) != target:
            raise ValueError(f"entry identity disagrees with its path: {relative}")
        arrived_entries[target] = (relative, entry)
    projection_errors = registration_projection.validate_projections(
        root,
        [(relative, entry) for relative, entry in arrived_entries.values()],
        base=previous["database_commit"],
        changed_paths=frozenset(projection_changes),
    )
    if projection_errors:
        raise ValueError(
            "accepted registration projections are not an exact transition: "
            + projection_errors[0]
        )

    touched: list[patch_surfaces.Touched] = []
    identities: dict[str, dict[str, Any]] = {}
    for identifier in sorted(changed):
        identity, every = _versions_of(root, identifier)
        identities[identifier] = identity
        # The parent version document, fetched by exact path, decides which
        # historical rows remain active. Until it is read, `every` is enough
        # to name every derived document this result can touch.
        touched.append(patch_surfaces.Touched(identifier, every, list(every)))

    by_target = {
        (identifier, int(summary["version"])): (summary, entry)
        for item in touched
        for summary, entry in item.every
        for identifier in [item.id]
    }
    # The projection is sufficient for historical presentation rows. The new
    # immutable entry itself is opened for copying, schema checks, recent and
    # search, and replaces its projection view in the touched result.
    for target in sorted(arrived):
        relative, entry = arrived_entries[target]
        pair = (_summary(entry, relative), entry)
        by_target[target] = pair
        item = next(item for item in touched if item.id == target[0])
        item.every = [pair if summary.get("version") == target[1] else (summary, view)
                      for summary, view in item.every]
        item.active = [pair if summary.get("version") == target[1] else (summary, view)
                       for summary, view in item.active]
    for item in touched:
        if item.current is not None:
            current_target = (item.id, int(item.current[0]["version"]))
            if current_target not in arrived:
                print(
                    "staging everything: incremental current version is not an "
                    "arriving immutable entry "
                    f"({current_target[0]}-v{current_target[1]})"
                )
                return None
    staging = [by_target[target] for target in sorted(arrived)]
    added_records = len(arrived)
    registrations = [
        (identifier, identities[identifier])
        for identifier in sorted({identifier for identifier, _version in arrived})
    ]
    return Plan(
        str(previous["release"]),
        touched,
        staging,
        frozenset(arrived),
        added_records,
        registrations,
    )


def _stage_incremental(
    root: pathlib.Path,
    output: pathlib.Path,
    previous: dict[str, Any],
    prior: pathlib.Path,
    ready: Plan,
) -> list[pathlib.Path]:
    """Write the objects this release changes, and nothing else.

    Every entry file opened here belongs to a result the release touched. No
    document whose size is the registry's is read, built or hashed, and the
    delta names only what was written.
    """
    ready.bind_prior_versions(prior)
    for summary, entry in ready.staging:
        _copy_record(root, output, summary, entry)
    _stage_schema(root, output)
    _check_against_schema(output, [summary for summary, _entry in ready.staging])
    _copy_relative(root, output, "LICENSE")
    _write_version_indexes(
        output,
        {
            item.id: [summary for summary, _entry in item.active]
            for item in ready.touched
        },
    )
    registrations_by_id = dict(ready.registrations)
    build_registration_lookups.patch(
        output,
        prior,
        ready.newly_active_registrations(),
        [
            (
                str(entry["id"]),
                registrations_by_id[str(entry["id"])],
                str(entry["source"]["commit"]),
            )
            for _summary, entry in ready.staging
        ],
    )
    codes = patch_surfaces.patch(
        output, prior, ready.touched, parent_records=int(previous["records"]["count"])
    )
    build_feeds(output, codes)
    # One append-only postings sequence per word, and the only thing this file
    # has to know about searching. It appends to the open page of each word the
    # release adds and scans the sequences of each word it withdraws, so a
    # publication writes two objects per word of the record it adds however
    # many results the registry holds. See tools/build_search.py.
    retired = patch_search(output, prior, ready.staging, [])
    return _delta(
        output,
        root,
        parent=ready.parent,
        previous=previous,
        withdrawals=[],
        retired=retired,
        count=int(previous["records"]["count"]) + ready.added,
    )


def _stage_full(
    root: pathlib.Path,
    output: pathlib.Path,
) -> list[pathlib.Path]:
    """Build the whole dataset, which is the O(S) path and the only one.

    A full rebuild states the whole set, so the root can be taken over it
    directly and the publisher has something to reconcile a listing against.
    """
    results = registration_projection.all_result_documents(root)
    summaries = [
        registration_projection.public_summary(row, str(result["id"]))
        for result in results
        for row in result["versions"]
    ]

    entries_by_target: dict[tuple[str, int], tuple[dict[str, Any], dict[str, Any]]] = {}
    for summary in summaries:
        relative = summary.get("path")
        match = ENTRY_PATH_RE.fullmatch(relative) if isinstance(relative, str) else None
        if match is None:
            raise ValueError(f"registration projection names an unexpected path: {relative!r}")
        entry = _load_json(root / relative)
        target = (match.group(1), int(match.group(2)))
        if not isinstance(entry, dict) or (entry.get("id"), entry.get("version")) != target:
            raise ValueError(f"entry identity disagrees with its path: {relative}")
        entries_by_target[target] = (summary, entry)

    rows, takedown_errors = load_takedowns(root, entries_by_target)
    if takedown_errors:
        raise ValueError("invalid takedowns.json:\n" + "\n".join(takedown_errors))
    taken_down = {(row["id"], row["version"]) for row in rows}

    active_summaries: list[dict[str, Any]] = []
    active_entries: list[dict[str, Any]] = []
    for target, (summary, entry) in entries_by_target.items():
        if target in taken_down:
            continue
        active_summaries.append(summary)
        active_entries.append(entry)
        _copy_record(root, output, summary, entry)

    # Staged, never published. Nothing derived reads it -- the pages
    # are built from the records already in hand -- but it is the one document
    # that states the whole active set, and the reconciliation the weekly sweep
    # does is written against it.
    (output / "index.json").write_text(
        json.dumps(
            {
                "entries": active_summaries,
                "schema_version": 3,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    _write_tombstones(output, rows)
    _stage_schema(root, output)
    _check_against_schema(output, active_summaries)
    _copy_relative(root, output, "LICENSE")
    by_id: dict[str, list[dict[str, Any]]] = {
        identifier: [] for identifier, _version in entries_by_target
    }
    for summary in active_summaries:
        by_id[str(summary["id"])].append(summary)
    _write_version_indexes(output, by_id)
    build_registration_lookups.build(
        output,
        results,
        active_entries,
        [entry for _summary, entry in entries_by_target.values()],
    )
    # Seeded from every version the database holds and filled from the ones
    # being served, so a page a withdrawal emptied is served empty rather than
    # disappearing -- the same answer the tombstone gives, and the only one a
    # release that patches pages can agree with byte for byte.
    build_browse(output, active_summaries, [identifier for identifier, _v in entries_by_target])
    # Built from the records already in memory, so the subject pages cost no
    # reads of their own.
    build_subjects(
        output,
        active_entries,
        active_summaries,
        [entry for _summary, entry in entries_by_target.values()],
    )
    # Same records, same module deciding what is current and when it was
    # published, so the landing page cannot come to disagree with the feeds and
    # the subject pages about what is newest.
    build_recent(output, active_entries)
    # Last, and from the staged pages rather than from the records: a feed is
    # the same answer as the page beside it in the format an aggregator reads,
    # so rendering it from that page is what stops the two disagreeing, and it
    # is what removes the second whole-registry pass a publication used to
    # make after this one.
    build_feeds(output)
    # Stated outright from every record being served, which is what a rebuild
    # is for: a word whose last result was withdrawn is absent here rather than
    # left with a head saying it has none.
    build_search(output, list(zip(active_summaries, active_entries)))
    return _delta(
        output,
        root,
        parent=None,
        previous=None,
        withdrawals=[],
        # A full rebuild states the whole dataset, so the publisher reconciles
        # it against a listing and needs nothing declared.
        retired=[],
        count=len(active_summaries),
    )


def stage_public(
    root: pathlib.Path,
    output: pathlib.Path,
    *,
    previous: dict[str, Any] | None = None,
    full: bool = False,
    prior: pathlib.Path | None = None,
    require_incremental: bool = False,
) -> list[pathlib.Path]:
    """Build the next release, and describe it as a delta from the last one.

    With a `previous` authenticated base whose commit this checkout can still
    reach and the `prior` documents that release is serving, this writes only
    what changed: the records that arrived, the pages they appear on, and the
    two documents above each of those pages. The append-only invariant is what
    makes a git diff a complete answer to what the release adds.

    Without one -- a first publication, an unknown parent, a parent in another
    surface layout, a prior page that cannot be patched into what a rebuild
    would produce, `--full` -- everything is staged and the delta declares the
    whole set. Falling back is always correct and merely slow, so every
    uncertainty falls back. A proven-invalid projection transition is refused
    rather than laundered through a full rebuild. An uncommitted registration
    authority is different too: neither path may consume projection bytes that
    are not in the declared HEAD commit.
    """
    root = root.resolve()
    output = output.resolve()
    if (root / ".git").exists():
        try:
            changed_records.require_clean(root, ("registrations",))
        except changed_records.CannotTell as reason:
            raise ValueError(
                f"cannot stage an uncommitted registration authority: {reason}"
            ) from reason
    ready = None
    if prior is not None:
        ready = plan(root, previous, full)
    if ready is not None:
        output.mkdir(parents=True, exist_ok=False)
        try:
            return _stage_incremental(root, output, previous, prior.resolve(), ready)
        except patch_surfaces.Rebuild as reason:
            print(f"staging everything: {reason}")
            shutil.rmtree(output)
            if require_incremental:
                raise FullCheckoutRequired(str(reason)) from reason
    if require_incremental:
        raise FullCheckoutRequired("the release is not a closed incremental update")
    output.mkdir(parents=True, exist_ok=False)
    return _stage_full(root, output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=pathlib.Path, default=pathlib.Path("."))
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument(
        "--previous",
        type=pathlib.Path,
        help="the authenticated base of the release currently being served, so that only what "
        "has changed since it is staged. Without one, everything is.",
    )
    parser.add_argument(
        "--prior",
        type=pathlib.Path,
        help="a directory of the published documents this release patches, fetched "
        "by name from the release being served. Without one, everything is rebuilt.",
    )
    parser.add_argument(
        "--plan",
        type=pathlib.Path,
        help="write the published documents this release needs and does not yet "
        "have, one per line, and stage nothing. This step has no bucket "
        "credentials and needs none: it reads the git diff, touched projections, "
        "the committed takedown blob identity, and whatever --prior already holds.",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="stage the whole dataset even when an incremental release is possible",
    )
    parser.add_argument(
        "--require-incremental",
        action="store_true",
        help="stage only a closed incremental release; exit "
        f"{FULL_REQUIRED_EXIT} without an output tree when safe staging requires "
        "a complete checkout",
    )
    args = parser.parse_args()
    if args.full and args.require_incremental:
        parser.error("--full and --require-incremental are mutually exclusive")
    if args.plan is not None and args.require_incremental:
        parser.error("--require-incremental stages an output and cannot be used with --plan")
    previous = None
    if args.previous is not None and args.previous.is_file() and args.previous.stat().st_size:
        try:
            raw = args.previous.read_bytes()
            previous = release_delta.parse_base(raw)
            if raw != release_delta.canonical_base_bytes(previous):
                raise ValueError("publication base is not in canonical byte form")
        except (json.JSONDecodeError, ValueError) as error:
            # Not fatal. An unreadable parent means this release cannot be
            # described as a difference from it, which is the same situation as
            # having no parent at all, and the answer to that is a full rebuild.
            print(f"staging everything: the previous publication base is unreadable: {error}")
    if args.plan is not None:
        ready = plan(args.root.resolve(), previous, args.full)
        reason = None
        try:
            wanted = [] if ready is None else ready.prior_paths(args.prior)
        except patch_surfaces.Rebuild as error:
            # The second planning pass reads search heads to discover page
            # numbers. A malformed head already proves incremental staging is
            # unsafe, so there is no third fetch to ask for; the staging pass
            # will see the same head and take the ordinary full fallback.
            ready = None
            wanted = []
            reason = error
        args.plan.write_text(
            "" if ready is None else "\n".join(wanted) + "\n", encoding="utf-8"
        )
        print(
            f"a full rebuild: {reason}"
            if reason is not None
            else ("a full rebuild" if ready is None else f"{len(wanted)} prior documents")
        )
        return 0
    if args.output is None:
        parser.error("--output is required unless --plan is given")
    try:
        written = stage_public(
            args.root,
            args.output,
            previous=previous,
            full=args.full,
            prior=args.prior,
            require_incremental=args.require_incremental,
        )
    except FullCheckoutRequired as reason:
        print(f"complete checkout required: {reason}")
        return FULL_REQUIRED_EXIT
    print(f"staged {len(written)} public files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
