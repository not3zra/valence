"""Python 3.12 tooling baseline and reproducible installs (issue #100).

The seams are the checked-in tooling contract — project metadata, lockfile,
Docker build, and developer documentation — that the lint, typecheck, test,
and container commands all consume.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _toml(name: str) -> dict:
    return tomllib.loads(_read(name))


def test_project_declares_python_312_baseline() -> None:
    config = _toml("pyproject.toml")

    assert config["project"]["requires-python"] == ">=3.12"
    assert config["tool"]["ruff"]["target-version"] == "py312"
    assert config["tool"]["mypy"]["python_version"] == "3.12"


def test_lockfile_tracks_the_python_312_baseline() -> None:
    lock = _toml("uv.lock")
    gitignore = _read(".gitignore").splitlines()

    assert lock["requires-python"] == ">=3.12"
    assert "/uv.lock" not in gitignore


def test_docker_installs_from_the_locked_dependency_set() -> None:
    dockerfile = _read("Dockerfile")
    copies_lockfile = any(
        "uv.lock" in line
        for line in dockerfile.splitlines()
        if line.startswith("COPY ")
    )

    assert "FROM python:3.12-slim" in dockerfile
    assert copies_lockfile
    assert "uv sync --frozen" in dockerfile


def test_readme_documents_the_baseline_and_developer_commands() -> None:
    readme = _read("README.md")

    assert "Python 3.12+" in readme
    for command in (
        "uv sync",
        "uv run ruff check .",
        "uv run mypy src",
        "uv run pytest",
        "docker compose up",
    ):
        assert command in readme, f"missing developer command: {command}"
