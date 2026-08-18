#!/usr/bin/env python3
"""Build the bounded, self-contained projections used by the landing page.

`recent.json` names the newest current versions without making a browser read
the registry.  Each row is also everything the landing card renders, so the
browser does not turn a bounded page into one record request per result.

`recent-renders.json` names the same results again, carrying only the content
address of each one's Verso rendering.  It is a second document rather than
three more keys on a card because the landing page reads it lazily, on the
first hover, and a reader who never hovers should not pay for it.  Keeping it
separate is also what makes it deployable: both sides of `recent.json` are
closed shapes, so a key added there is rejected by the site already being
served, while a document nothing has asked for yet breaks nothing.

Neither projection is another record.  Both are rebuilt from canonical,
validated entries already in the stager's memory; no projected field is read
from the private index or maintained separately.  Their deliberately small
nested objects match the parts of a record the card consumes and nothing else.

The two are built from one selection, and the render document is built from the
*finished* recent page rather than from the selection again, so they cannot come
to name different results.
"""

from __future__ import annotations

import collections
import json
import pathlib
import re
from typing import Any

from selection import ARXIV_RE, MSC_RE, latest_entries, parsed, published_at, rows_newest_first

SCHEMA_VERSION = 2
RECENT_PATH = "recent.json"
RECENT_RENDERS_PATH = "recent-renders.json"
# A hard bound, not a page size. Unbounded this is `index.json` again under
# another name: the one served object whose size is the registry's, fetched by
# every visitor. The cap keeps the newest, because a page of what is new that
# dropped the newest would be worse than no page at all, and browsing is the
# surface that lists everything.
RECENT_ITEMS = 200

IDENTIFIER_RE = re.compile(r"PALOMAR-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}")
REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
ARCHIVE_REPOSITORY_RE = re.compile(r"PalomarArchive/[A-Za-z0-9_.-]+")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
TREE_HASH_RE = re.compile(r"[0-9a-f]{64}")
RENDER_ROW_KEYS = {"id", "version", "artifact_tree_sha256"}
ROW_KEYS = {
    "abstract",
    "authors",
    "classification",
    "formalization",
    "id",
    "path",
    "preservation",
    "published_at",
    "source",
    "status",
    "title",
    "trust",
    "version",
    "versions",
}


def validate_entry_schema_for_recent(schema: object, name: str) -> None:
    """Check only that the canonical schema guarantees what projection reads.

    Canonical entries are still validated by jsonschema. This is deliberately
    not another entry validator: it makes the small structural dependency of
    `row` and `render_hash` explicit, so making a projected field optional in a
    future canonical schema fails publication instead of becoming a late
    KeyError.
    """
    def at(path: tuple[str, ...]) -> dict[str, Any]:
        node = schema
        for key in path:
            if not isinstance(node, dict) or key not in node:
                raise ValueError(f"{name}: cannot establish {'.'.join(path)}")
            node = node[key]
        if not isinstance(node, dict):
            raise ValueError(f"{name}: {'.'.join(path) or 'entry'} is not an object schema")
        return node

    requirements = {
        (): {
            "id", "version", "status", "title", "abstract", "authors",
            "classification", "formalization", "trust", "source", "registered_at",
            "preservation", "challenge_render",
        },
        ("properties", "authors", "items"): {"name"},
        ("properties", "classification"): {"arxiv", "msc2020"},
        ("properties", "formalization"): {"theorem_names"},
        ("properties", "trust"): {"level"},
        ("properties", "challenge_render"): {"artifact_tree_sha256"},
        ("properties", "source"): {"repository", "commit"},
        ("properties", "preservation"): {"repositories"},
        (
            "properties", "preservation", "properties", "repositories", "items"
        ): {"source_repository", "commit", "fork_repository"},
    }

    for path, required in requirements.items():
        declared = at(path).get("required")
        if not isinstance(declared, list) or not required.issubset(set(declared)):
            missing = sorted(required - set(declared or []))
            where = ".".join(path) or "entry"
            raise ValueError(f"{name}: {where} does not require {missing}")

    project_path = at(("properties", "source", "properties", "project_path"))
    if project_path.get("type") != "string":
        raise ValueError(f"{name}: source.project_path must be a string when present")


def _source_preservation(entry: dict[str, Any]) -> dict[str, Any]:
    """The one archive mapping the card can link to, not the dependency graph."""
    preservation = entry["preservation"]
    source = entry["source"]
    matches = [
        mapping
        for mapping in preservation["repositories"]
        if mapping["source_repository"].casefold() == source["repository"].casefold()
        and mapping["commit"] == source["commit"]
    ]
    if len(matches) != 1:
        # Canonical validation establishes this before staging. Keep the
        # projection's dependency explicit too: accepting no match here would
        # quietly publish a card with a broken archive link.
        raise ValueError(
            f"{entry['id']}-v{entry['version']} has {len(matches)} source archive mappings"
        )
    mapping = matches[0]
    return {
        "repositories": [
            {
                "source_repository": mapping["source_repository"],
                "commit": mapping["commit"],
                "fork_repository": mapping["fork_repository"],
            }
        ]
    }


def row(entry: dict[str, Any], versions: int) -> dict[str, Any]:
    """Project one validated canonical current entry into one landing card."""
    source = entry["source"]
    return {
        "id": entry["id"],
        "version": entry["version"],
        "status": entry["status"],
        "title": entry["title"],
        "path": f"entries/{entry['id']}-v{entry['version']}.json",
        "abstract": entry["abstract"],
        "authors": [{"name": author["name"]} for author in entry["authors"]],
        "classification": {
            "arxiv": list(entry["classification"]["arxiv"]),
            "msc2020": list(entry["classification"]["msc2020"]),
        },
        "formalization": {
            "theorem_names": list(entry["formalization"]["theorem_names"]),
        },
        "trust": {"level": entry["trust"]["level"]},
        "source": {
            "repository": source["repository"],
            "commit": source["commit"],
            # Always present in the projection. `null` means repository root,
            # so consumers have one shape rather than optional-key aliases.
            "project_path": source.get("project_path"),
        },
        "preservation": _source_preservation(entry),
        "published_at": published_at(entry),
        "versions": versions,
    }


def render_hash(entry: dict[str, Any]) -> str:
    """The one part of a record's rendering the landing page needs to frame it.

    Not the whole `challenge_render`. The artifact path is derivable from the
    identifier, the version and this hash, and the format and entrypoint are
    constants the browser asserts rather than reads, so publishing them per row
    would be publishing the same two strings two hundred times.
    """
    return entry["challenge_render"]["artifact_tree_sha256"]


def render_page(
    page: list[dict[str, Any]], hashes: dict[tuple[str, int], str]
) -> list[dict[str, Any]]:
    """The render hashes of exactly the results on a finished recent page.

    Built from the page rather than from the selection that produced it, so the
    two documents cannot come to disagree about which results they describe. A
    row on the page whose hash is not to hand is refused rather than dropped:
    silently omitting one publishes a landing page whose hover does nothing for
    a result, with nothing anywhere saying why.
    """
    rows = []
    for item in page:
        key = (item["id"], item["version"])
        if key not in hashes:
            raise ValueError(
                f"{RECENT_RENDERS_PATH}: no render hash for {key[0]}-v{key[1]}"
            )
        rows.append(
            {"id": key[0], "version": key[1], "artifact_tree_sha256": hashes[key]}
        )
    # Identifier order, not the page's newest-first order. One row per result
    # here as there, so ordering by identifier is total, and a document whose
    # order is a function of its own rows can be checked without the page.
    rows.sort(key=lambda item: item["id"])
    return rows


def _string(
    value: object, where: str, *, nonempty: bool = True, document: str = RECENT_PATH
) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise ValueError(f"{document}: {where} must be a string")
    return value


def _strings(value: object, where: str, *, pattern: re.Pattern[str] | None = None) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{RECENT_PATH}: {where} must be a non-empty array")
    result: list[str] = []
    for position, item in enumerate(value):
        text = _string(item, f"{where}[{position}]")
        if pattern is not None and not pattern.fullmatch(text):
            raise ValueError(f"{RECENT_PATH}: {where}[{position}] is malformed")
        result.append(text)
    if len(set(result)) != len(result):
        raise ValueError(f"{RECENT_PATH}: {where} must be distinct")
    return result


def _exact(
    value: object, keys: set[str], where: str, *, document: str = RECENT_PATH
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{document}: {where} has an invalid shape")
    return value


def validate_recent(document: object) -> dict[str, Any]:
    """Validate the complete public contract, including its semantic bounds."""
    page = _exact(document, {"schema_version", "entries"}, "document")
    if (
        type(page["schema_version"]) is not int
        or page["schema_version"] != SCHEMA_VERSION
    ):
        raise ValueError(f"{RECENT_PATH}: unsupported schema version")
    entries = page["entries"]
    if not isinstance(entries, list) or len(entries) > RECENT_ITEMS:
        raise ValueError(f"{RECENT_PATH}: entries exceeds its {RECENT_ITEMS}-row bound")

    identifiers: set[str] = set()
    for position, value in enumerate(entries):
        where = f"entries[{position}]"
        item = _exact(value, ROW_KEYS, where)
        identifier = _string(item["id"], f"{where}.id")
        if not IDENTIFIER_RE.fullmatch(identifier) or identifier in identifiers:
            raise ValueError(f"{RECENT_PATH}: {where}.id is malformed or duplicated")
        identifiers.add(identifier)
        version = item["version"]
        versions = item["versions"]
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version < 1
            or not isinstance(versions, int)
            or isinstance(versions, bool)
            or versions < 1
        ):
            raise ValueError(f"{RECENT_PATH}: {where} has invalid version counts")
        if item["status"] != "registered":
            raise ValueError(f"{RECENT_PATH}: {where}.status must be registered")
        _string(item["title"], f"{where}.title")
        _string(item["abstract"], f"{where}.abstract")
        if item["path"] != f"entries/{identifier}-v{version}.json":
            raise ValueError(f"{RECENT_PATH}: {where}.path does not name its entry")
        stamp = _string(item["published_at"], f"{where}.published_at")
        parsed(stamp)

        authors = item["authors"]
        if not isinstance(authors, list) or not authors:
            raise ValueError(f"{RECENT_PATH}: {where}.authors must be a non-empty array")
        for author_position, author in enumerate(authors):
            author = _exact(author, {"name"}, f"{where}.authors[{author_position}]")
            _string(author["name"], f"{where}.authors[{author_position}].name")

        classification = _exact(
            item["classification"], {"arxiv", "msc2020"}, f"{where}.classification"
        )
        _strings(classification["arxiv"], f"{where}.classification.arxiv", pattern=ARXIV_RE)
        _strings(
            classification["msc2020"], f"{where}.classification.msc2020", pattern=MSC_RE
        )
        formalization = _exact(
            item["formalization"], {"theorem_names"}, f"{where}.formalization"
        )
        _strings(formalization["theorem_names"], f"{where}.formalization.theorem_names")
        trust = _exact(item["trust"], {"level"}, f"{where}.trust")
        level = trust["level"]
        if not isinstance(level, str) or level not in {"high", "qualified"}:
            raise ValueError(f"{RECENT_PATH}: {where}.trust.level is invalid")

        source = _exact(
            item["source"], {"repository", "commit", "project_path"}, f"{where}.source"
        )
        repository = _string(source["repository"], f"{where}.source.repository")
        commit = _string(source["commit"], f"{where}.source.commit")
        if not REPOSITORY_RE.fullmatch(repository) or not COMMIT_RE.fullmatch(commit):
            raise ValueError(f"{RECENT_PATH}: {where}.source is malformed")
        project_path = source["project_path"]
        if project_path is not None:
            _string(project_path, f"{where}.source.project_path")

        preservation = _exact(
            item["preservation"], {"repositories"}, f"{where}.preservation"
        )
        repositories = preservation["repositories"]
        if not isinstance(repositories, list) or len(repositories) != 1:
            raise ValueError(
                f"{RECENT_PATH}: {where}.preservation must contain one source mapping"
            )
        mapping = _exact(
            repositories[0],
            {"source_repository", "commit", "fork_repository"},
            f"{where}.preservation.repositories[0]",
        )
        if (
            not isinstance(mapping["source_repository"], str)
            or mapping["source_repository"].casefold() != repository.casefold()
            or mapping["commit"] != commit
            or not isinstance(mapping["fork_repository"], str)
            or not ARCHIVE_REPOSITORY_RE.fullmatch(mapping["fork_repository"])
        ):
            raise ValueError(f"{RECENT_PATH}: {where}.preservation does not match source")

    if entries != rows_newest_first(entries):
        raise ValueError(f"{RECENT_PATH}: entries are not newest first")
    return page


def validate_recent_renders(document: object) -> dict[str, Any]:
    """Validate the complete public contract, including its semantic bounds."""
    where_doc = RECENT_RENDERS_PATH
    page = _exact(document, {"schema_version", "renders"}, "document", document=where_doc)
    if (
        type(page["schema_version"]) is not int
        or page["schema_version"] != SCHEMA_VERSION
    ):
        raise ValueError(f"{where_doc}: unsupported schema version")
    renders = page["renders"]
    if not isinstance(renders, list) or len(renders) > RECENT_ITEMS:
        raise ValueError(f"{where_doc}: renders exceeds its {RECENT_ITEMS}-row bound")

    previous = ""
    for position, value in enumerate(renders):
        where = f"renders[{position}]"
        item = _exact(value, RENDER_ROW_KEYS, where, document=where_doc)
        identifier = _string(item["id"], f"{where}.id", document=where_doc)
        if not IDENTIFIER_RE.fullmatch(identifier):
            raise ValueError(f"{where_doc}: {where}.id is malformed")
        # Strictly increasing, which is distinctness and a canonical order in
        # one rule. One row per result, as on the page this accompanies, so a
        # repeated identifier means this is not the projection it says it is.
        if identifier <= previous:
            raise ValueError(f"{where_doc}: renders are not in increasing identifier order")
        previous = identifier
        version = item["version"]
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError(f"{where_doc}: {where}.version is invalid")
        tree = _string(
            item["artifact_tree_sha256"],
            f"{where}.artifact_tree_sha256",
            document=where_doc,
        )
        if not TREE_HASH_RE.fullmatch(tree):
            raise ValueError(f"{where_doc}: {where}.artifact_tree_sha256 is malformed")
    return page


def write_recent_renders(
    output: pathlib.Path, renders: list[dict[str, Any]]
) -> pathlib.Path:
    document = validate_recent_renders(
        {"schema_version": SCHEMA_VERSION, "renders": renders}
    )
    target = output / RECENT_RENDERS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def write_recent(output: pathlib.Path, page: list[dict[str, Any]]) -> pathlib.Path:
    document = validate_recent({"schema_version": SCHEMA_VERSION, "entries": page})
    target = output / RECENT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def build_recent(
    output: pathlib.Path, entries: list[dict[str, Any]]
) -> tuple[pathlib.Path, pathlib.Path]:
    """Build from every validated active version already read by the stager."""
    versions = collections.Counter(str(entry["id"]) for entry in entries)
    current = latest_entries(entries)[:RECENT_ITEMS]
    page = [row(entry, versions[str(entry["id"])]) for entry in current]
    hashes = {
        (entry["id"], entry["version"]): render_hash(entry) for entry in current
    }
    return write_recent(output, page), write_recent_renders(
        output, render_page(page, hashes)
    )
