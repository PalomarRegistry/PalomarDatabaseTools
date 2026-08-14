"""Run a reviewed tool without depending on the source checkout layout."""

from __future__ import annotations

import importlib
import sys

COMMANDS = frozenset(
    {
        "check-append-only",
        "check-published",
        "check-source-availability",
        "lock-tool-requirements",
        "publication-evidence",
        "public-source-availability",
        "publish-availability",
        "publish-availability-targets",
        "publish-snapshot",
        "smoke-source-availability",
        "source-availability-targets",
        "stage-public",
        "validate",
    }
)


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        available = ", ".join(sorted(COMMANDS))
        print(f"usage: palomar-database-tools <command> [args...]\ncommands: {available}", file=sys.stderr)
        return 2
    module_name = sys.argv.pop(1).replace("-", "_")
    module = importlib.import_module(module_name)
    result = module.main()
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
