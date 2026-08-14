#!/usr/bin/env bash
set -euo pipefail

output="${1:-dist/runtime}"
if [ -e "$output" ]; then
  echo "output already exists: $output" >&2
  exit 1
fi

epoch="$(git show -s --format=%ct HEAD 2>/dev/null || date +%s)"
export SOURCE_DATE_EPOCH="$epoch"
temporary="$(mktemp -d)"
trap 'rm -rf "$temporary"' EXIT

python -m build --wheel --outdir "$temporary/first"
python -m build --wheel --outdir "$temporary/second"
first="$(find "$temporary/first" -maxdepth 1 -name '*.whl' -type f -print -quit)"
second="$(find "$temporary/second" -maxdepth 1 -name '*.whl' -type f -print -quit)"
cmp "$first" "$second"

mkdir -p "$output"
cp "$first" "$output/"
cp requirements-tools.txt "$output/"
wheel="$(basename "$first")"
(
  cd "$output"
  sha256sum "$wheel" requirements-tools.txt > SHA256SUMS
)

python - <<'PY' "$output/manifest.json" "$(basename "$first")" "$epoch"
import json
import pathlib
import subprocess
import sys

target, wheel, epoch = sys.argv[1:]
manifest = {
    "schema_version": 1,
    "repository": "PalomarRegistry/PalomarDatabaseTools",
    "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    "source_date_epoch": int(epoch),
    "python": "3.11",
    "wheel": wheel,
}
pathlib.Path(target).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
PY

echo "runtime bundle: $output"
