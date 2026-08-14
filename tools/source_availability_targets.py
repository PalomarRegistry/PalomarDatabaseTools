#!/usr/bin/env python3
"""Build the public, observation-free source-availability target set."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import subprocess
from typing import Any

from check_source_availability import preservation_rows


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def target_document(root: pathlib.Path, *, generated_at: str | None = None) -> dict[str, Any]:
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return {
        "schema_version": 1,
        "generated_at": generated_at or utc_now(),
        "database_commit": commit,
        "targets": preservation_rows(root),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=pathlib.Path, default=pathlib.Path("."))
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args(argv)
    document = target_document(args.root)
    args.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {len(document['targets'])} public source-availability targets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
