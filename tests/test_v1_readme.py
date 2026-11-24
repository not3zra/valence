"""README contract for portable Valence v1 (issue #102).

The seam is the checked-in README: a new operator must be able to install,
configure and run an Instance from it without reading the source.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _readme() -> str:
    return (ROOT / "README.md").read_text(encoding="utf-8")


def test_readme_covers_local_and_compose_setup_with_migrations_and_env() -> None:
    readme = _readme()

    assert "Docker Compose" in readme
    assert "docker compose up" in readme
    assert "POSTGRES_PASSWORD" in readme
    assert "valence init" in readme
    assert "valence setup" in readme
    assert "alembic upgrade head" in readme
    assert "DATABASE_URL" in readme
    assert ".env.example" in readme
    assert "environment variables" in readme.lower()


def test_readme_documents_every_public_surface() -> None:
    readme = _readme()

    for documented in (
        "REST",
        "/api/v1/intake",
        "/api/v1/orders",
        "/api/v1/loading-lists",
        "Idempotency-Key",
        "MCP",
        "/mcp",
        "valence mcp --transport stdio",
        "webhook",
        "X-Hub-Signature-256",
        "approvals",
        "approve",
        "reject",
        "confirm: true",
        "vouchers",
        "download_voucher",
        "Loading List",
        "late",
        "billing",
        "Tally",
        "valence doctor",
        "valence cutoff",
    ):
        assert documented in readme, f"README does not document {documented!r}"


def test_readme_drops_stale_claims() -> None:
    readme = _readme()

    for stale in (
        "393",
        "Cloud Run",
        "Twilio",
        "preview revision",
        "CI preview",
        "/api/roundtrip",
        "/api/voice/ingest",
        "/api/whatsapp/webhook",
        "/api/vouchers",
        "/api/cutoff",
        "ROUNDTRIP_TOKEN",
        "VOICE_INGEST_TOKEN",
        "CUTOFF_SECRET",
    ):
        assert stale not in readme, f"stale claim still present: {stale!r}"


def test_readme_links_only_to_committed_docs() -> None:
    links = re.findall(r"\]\(([^)]+)\)", _readme())
    gitignored = (
        "docs/",
        "CONTEXT.md",
        "AGENTS.md",
        "THREAT_MODEL.md",
        "handoff/",
        "ralph/",
    )

    for target in links:
        assert not any(target.startswith(path) for path in gitignored), (
            f"README links to gitignored documentation: {target!r}"
        )


def test_readme_points_at_the_verified_quickstart() -> None:
    readme = _readme()

    assert "scripts/e2e_harness.py" in readme
    assert "uv run python scripts/e2e_harness.py" in readme
