#!/usr/bin/env python3
"""Publish the public source-availability target document to its stable R2 key."""

from __future__ import annotations

import argparse
import json
import os
import pathlib

from publish_snapshot import BUCKET_DEFAULT, _put_object, _sha256, _verify

KEY = "public/source-availability-targets.json"
LIMIT = 5 * 1024 * 1024


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document", type=pathlib.Path)
    parser.add_argument("--bucket", default=os.environ.get("R2_BUCKET", BUCKET_DEFAULT))
    args = parser.parse_args(argv)
    raw = args.document.read_bytes()
    if len(raw) > LIMIT:
        parser.error(f"target document exceeds {LIMIT} bytes")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        parser.error("target document has an unsupported schema")
    if not isinstance(value.get("targets"), list):
        parser.error("target document has no targets array")
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    access_key = os.environ.get("R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY")
    if not all((account_id, access_key, secret_key)):
        parser.error("Cloudflare account and R2 credentials are required")
    import boto3

    client = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
    )
    digest = _sha256(raw)
    _put_object(client, args.bucket, KEY, raw, digest, "source-availability-targets.json")
    _verify(client, args.bucket, KEY, raw, digest)
    print("published source-availability targets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
