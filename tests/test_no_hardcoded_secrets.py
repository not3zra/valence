"""Guard against reintroducing retired credentials or deployment URLs (issue #51)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
SKIP_DIRS = {
    ".git",
    ".worktrees",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "build",
    "dist",
    "node_modules",
}
TEXT_SUFFIXES = {
    ".py",
    ".sh",
    ".md",
    ".yaml",
    ".yml",
    ".toml",
    ".json",
    ".example",
    ".provision",
    ".txt",
}
SPECIAL_FILES = {"Dockerfile", ".dockerignore", ".gitignore"}
RETIRED = (
    r"vlnc-[0-9a-f]{8,}",
    r"valence-\d{12}",
    r"valence-v4cfhykega",
    r"valence-505412",
    r"valence-demo",
    r"local-dev-(?:roundtrip|ingest)-token",
    r"local-dev-salt",
)


def _is_text(path: Path) -> bool:
    return path.name in SPECIAL_FILES or path.suffix in TEXT_SUFFIXES


def test_no_retired_secrets_or_urls() -> None:
    offenders: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.resolve() == SELF or not _is_text(path):
            continue
        relative = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in RETIRED:
            match = re.search(pattern, text)
            if match:
                offenders.append(f"{relative}: {match.group(0)}")
    assert not offenders, "Retired credentials/URLs found:\n" + "\n".join(offenders)
