"""Continuous integration contract for Portable Valence v1 (issue #101).

The seams are the checked-in CI files: the GitHub Actions workflow, the pytest
marker configuration, and the developer documentation that records the workflow
and the recommended branch protection. Each acceptance criterion is asserted
against those files so CI is verifiable without running it on GitHub.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_workflow_runs_lint_types_and_tests_on_python_312() -> None:
    workflow = _workflow()

    assert 'python-version: "3.12"' in workflow
    assert "ruff check" in workflow
    assert "mypy src" in workflow
    assert "uv run pytest" in workflow
    assert "pull_request:" in workflow


def test_workflow_runs_on_pushes_to_main() -> None:
    workflow = _workflow()

    assert "push:" in workflow
    assert "branches: [main]" in workflow


def test_workflow_has_a_postgres_service_job_that_runs_postgres_tests() -> None:
    workflow = _workflow()

    assert "services:" in workflow
    assert "image: postgres:" in workflow
    assert "TEST_POSTGRES_URL" in workflow
    assert "pytest -m postgres" in workflow
    assert "--health-cmd" in workflow


def test_end_to_end_harness_runs_against_sqlite_and_postgres() -> None:
    workflow = _workflow()

    assert workflow.count("scripts/e2e_harness.py") >= 2
    assert "--database-url" in workflow


def test_postgres_e2e_runs_before_the_conformance_tests() -> None:
    workflow = _workflow()

    assert workflow.index("End-to-end harness (Postgres)") < workflow.index(
        "Postgres conformance tests"
    )


def test_dependency_audit_fails_on_high_or_critical_advisories() -> None:
    workflow = _workflow()

    assert "pip-audit-extra" in workflow
    assert "--fail-level HIGH" in workflow
    assert "uv export --frozen" in workflow


def test_ci_uses_fake_providers_and_no_live_secrets() -> None:
    workflow = _workflow()

    assert "secrets." not in workflow
    assert "MODEL_PROVIDER" in workflow
    assert "fake" in workflow


def test_postgres_marker_is_registered() -> None:
    markers = tomllib.loads(_read("pyproject.toml"))["tool"]["pytest"]["ini_options"][
        "markers"
    ]

    assert any(marker.startswith("postgres:") for marker in markers)


def test_postgres_marker_selects_the_backend_conformance_tests() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", "postgres"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "test_store_conformance_across_sql_backends" in result.stdout
    assert "test_cutoff_works_on_both_sql_backends" in result.stdout
    assert "test_concurrent_claims_are_awarded_to_exactly_one_worker" in result.stdout


def test_readme_documents_ci_and_branch_protection() -> None:
    readme = _read("README.md")

    assert "Continuous Integration" in readme
    assert "branch protection" in readme
    assert ".github/workflows/ci.yml" in readme
