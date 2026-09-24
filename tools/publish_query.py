"""Stream derived query input through the Worker's authenticated operation.

No database credential leaves the Worker. Every request and response has an
explicit byte bound. Normal publication writes only changed results.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

from query_projection import QUERY_PATH, encoded

MAX_BYTES = 256 * 1024


class QueryPublisher:
    def __init__(self, base: str, token: str, release: str, full: bool):
        url = urllib.parse.urlsplit(base)
        if url.scheme != "https" or url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
            raise ValueError("query API must be an HTTPS origin")
        if len(token) < 32:
            raise ValueError("PALOMAR_QUERY_UPDATE_TOKEN is missing or too short")
        self.url = base.rstrip("/") + "/_operations/results"
        self.token, self.release, self.full = token, release, full
        self.build = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        self.results = 0

    def send(self, operation: dict[str, Any]) -> dict[str, Any]:
        raw = encoded({**operation, "release": self.release, "full": self.full, "build": self.build}).encode()
        if len(raw) > MAX_BYTES:
            raise ValueError("query operation exceeds 256 KiB")
        request = urllib.request.Request(self.url, data=raw, method="PUT", headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
        })
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    body = response.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise RuntimeError("query operation returned an oversized response")
                return json.loads(body)
            except urllib.error.HTTPError as error:
                if (error.code < 500 and error.code != 429) or attempt == 2:
                    # Only known protocol error codes may reach a workflow log.
                    # Never print arbitrary response text, requests or headers.
                    code = None
                    try:
                        raw_error = error.read(MAX_BYTES + 1)
                        if len(raw_error) <= MAX_BYTES:
                            code = json.loads(raw_error).get("error")
                    except (ValueError, AttributeError, http.client.HTTPException, OSError):
                        pass
                    finally:
                        error.close()
                    known = {"invalid_update", "update_too_large", "missing_record", "rebuild_required", "index_unavailable"}
                    detail = f" ({code})" if isinstance(code, str) and code in known else ""
                    raise RuntimeError(f"query operation {operation['op']} failed: HTTP {error.code}{detail}") from None
                error.close()
            except (http.client.HTTPException, OSError, ValueError):
                if attempt == 2:
                    raise RuntimeError(f"query operation {operation['op']} failed after retries") from None
            time.sleep(attempt + 1)
        raise AssertionError("unreachable")

    def upload(self, site: pathlib.Path) -> None:
        self.send({"op": "begin"})
        self.results = 0
        with (site / QUERY_PATH).open("rb") as stream:
            while line := stream.readline(MAX_BYTES + 1):
                if len(line) > MAX_BYTES or not line.endswith(b"\n"):
                    raise ValueError("query input has an oversized or incomplete operation")
                operation = json.loads(line)
                if operation["op"] not in ("record", "chunk", "complete"):
                    raise ValueError("invalid query staging operation")
                self.send(operation)
                self.results += operation["op"] == "complete"

    def remove(self, identifier: str) -> None:
        self.send({"op": "remove", "id": identifier})

    def finish(self) -> None:
        self.send({"op": "finish", "results": self.results})

    def collect(self) -> None:
        while self.send({"op": "collect"}).get("more"):
            pass

    def reconcile(self, site: pathlib.Path) -> list[str]:
        expected = {}
        projects = set()
        with (site / QUERY_PATH).open() as stream:
            for line in stream:
                operation = json.loads(line)
                if operation["op"] == "record":
                    r = operation["record"]
                    expected[r["id"]] = r["fingerprint"]
                    projects.add(r["repository"])
        after = None
        observed = {}
        while True:
            page = self.send({"op": "export", **({"after": after} if after else {})})
            for entry in page["entries"]:
                if entry["id"] in observed:
                    raise RuntimeError("query reconciliation returned a duplicate result")
                observed[entry["id"]] = entry["fingerprint"]
            following = page.get("next")
            if following is None:
                break
            if after is not None and following <= after:
                raise RuntimeError("query reconciliation did not advance")
            after = following
        problems = [f"query index differs for {identifier}" for identifier in sorted(expected.keys() | observed.keys())
                    if expected.get(identifier) != observed.get(identifier)]
        metadata = self.send({"op": "inspect"})
        if metadata.get("results") != len(expected):
            problems.append("query index result count differs")
        if metadata.get("projects") != len(projects):
            problems.append("query index project count differs")
        if metadata.get("release") != self.release:
            problems.append("query index publication differs")
        return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=pathlib.Path, required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--api", default="https://data.palomar-registry.org")
    parser.add_argument("--reconcile", action="store_true")
    args = parser.parse_args()
    publisher = QueryPublisher(args.api, os.environ.get("PALOMAR_QUERY_UPDATE_TOKEN", ""), args.release, True)
    if args.reconcile:
        problems = publisher.reconcile(args.site)
        for problem in problems:
            print(problem)
        return bool(problems)
    publisher.upload(args.site)
    publisher.finish()
    publisher.collect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
