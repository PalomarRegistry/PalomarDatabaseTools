"""The installed runtime must report the identity carried by its wheel."""

from __future__ import annotations

import pathlib
import tomllib

import palomar_database_tools


def test_runtime_version_matches_project_metadata():
    root = pathlib.Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    assert palomar_database_tools.__version__ == project["project"]["version"]
