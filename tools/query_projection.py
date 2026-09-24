"""Bounded display summaries and complete, chunked search input.

The JSONL staging file is private publisher input, never an R2 public object.
Each line is one bounded operation; even one unusually large result streams.
"""
from __future__ import annotations

import collections
import copy
import hashlib
import json
import pathlib
from typing import Any, Iterable

from build_recent import row, render_hash
from search_tokens import STOPWORDS, tokens
from selection import latest_entries

QUERY_PATH = "query-input.jsonl"
SUMMARY_BYTES = 16 * 1024
CHUNK_BYTES = 64 * 1024


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def search_chunks(entry: dict[str, Any]) -> Iterable[str]:
    texts = [entry["title"], entry["abstract"], entry["source"]["repository"]]
    texts.extend(person["name"] for person in entry["authors"])
    texts.extend(entry["formalization"]["theorem_names"])
    # Keep chunking independent of field boundaries. Repeated words are harmless;
    # SQL counts distinct matching query words at the result, not chunk, level.
    chunk: list[str] = []
    size = 0
    for text in texts:
        for word in tokens(text):
            if word in STOPWORDS:
                continue
            if size + len(word) + 1 > CHUNK_BYTES:
                yield " ".join(chunk)
                chunk, size = [], 0
            chunk.append(word)
            size += len(word) + 1
    if chunk:
        yield " ".join(chunk)


def summary(entry: dict[str, Any], versions: int) -> dict[str, Any]:
    result = copy.deepcopy(row(entry, versions))
    result["preview"] = {"artifact_tree_sha256": render_hash(entry),
                         "version": entry.get("registry_correction", {}).get("based_on", {}).get("version", entry["version"])}
    result["abbreviated"] = False
    result["source_omitted"] = False
    if len(encoded(result).encode()) <= SUMMARY_BYTES:
        return result
    result["abbreviated"] = True
    # Abbreviate presentation only. Search is derived independently from entry.
    for limit in (1024, 256, 64):
        result["abstract"] = entry["abstract"][:limit] + "…"
        result["authors"] = [{"name": p["name"][:limit] + ("…" if len(p["name"]) > limit else "")}
                             for p in entry["authors"][:8]]
        result["formalization"]["theorem_names"] = [name[:limit] + ("…" if len(name) > limit else "")
            for name in entry["formalization"]["theorem_names"][:8]]
        if len(encoded(result).encode()) <= SUMMARY_BYTES:
            return result
    # URLs must never be truncated or changed to point at a different source.
    # A record with unusually large source names remains listable and searchable;
    # source controls are available on the linked complete record.
    result["source"] = None
    result["preservation"] = None
    result["source_omitted"] = True
    if len(encoded(result).encode()) > SUMMARY_BYTES:
        raise ValueError("bounded query summary construction failed")
    return result


def write_query(output: pathlib.Path, records: Iterable[tuple[dict[str, Any], int]]) -> None:
    with (output / QUERY_PATH).open("w", encoding="utf-8") as stream:
        for entry, versions in records:
            display = summary(entry, versions)
            header = {"id": entry["id"], "published_at": entry["registered_at"],
                      "fingerprint": hashlib.sha256(encoded(entry).encode()).hexdigest(),
                      "trust": entry["trust"]["level"], "repository": entry["source"]["repository"].lower(),
                      "classification": entry["classification"], "summary": encoded(display)}
            # Repository identity can be long in legacy schemas. Store a digest
            # for distinct-project counting; the complete name remains searchable.
            header["repository"] = hashlib.sha256(header["repository"].encode()).hexdigest()
            stream.write(encoded({"op": "record", "record": header}) + "\n")
            count = 0
            for count, chunk in enumerate(search_chunks(entry), 1):
                stream.write(encoded({"op": "chunk", "id": entry["id"], "position": count - 1, "tokens": chunk}) + "\n")
            stream.write(encoded({"op": "complete", "id": entry["id"], "chunks": count}) + "\n")


def write_full_query(output: pathlib.Path, entries: list[dict[str, Any]]) -> None:
    counts = collections.Counter(entry["id"] for entry in entries)
    write_query(output, ((entry, counts[entry["id"]]) for entry in latest_entries(entries)))
