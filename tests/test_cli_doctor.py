"""``valence doctor`` checks an Instance without echoing secrets (issue #97)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import pytest

from src.auth import AuthService
from src.cli import main
from src.config import settings
from src.sql_store import SqlAuthStore, SqlOrderStore

ROOT = Path(__file__).resolve().parents[1]
CHECK_NAMES = {
    "python",
    "configuration",
    "database",
    "migrations",
    "model provider",
    "voucher store",
    "tally",
    "whatsapp",
    "mcp",
}


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'doctor.db'}"


def _migrate(url: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": url},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _configure_tally(url: str) -> None:
    async def configure() -> None:
        store = SqlOrderStore(url)
        auth = AuthService(SqlAuthStore(engine=store.engine))
        await auth.set_integration("tally", {"url": "http://tally.local/import"})
        await auth.set_integration(
            "meta",
            {
                "access_token": "fake-meta-token",
                "app_secret": "fake-app-secret",
                "phone_number_id": "fake-phone-id",
                "verify_token": "fake-verify-token",
            },
        )
        await auth.set_integration("gemini", {"api_key": "fake-provider-key"})
        await store.engine.dispose()

    asyncio.run(configure())


@pytest.fixture
def healthy(tmp_path):
    url = _sqlite_url(tmp_path)
    _migrate(url)
    _configure_tally(url)
    vouchers = tmp_path / "vouchers"
    vouchers.mkdir()
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(settings, "model_provider", "fake"))
        stack.enter_context(mock.patch.object(settings, "voucher_dir", str(vouchers)))
        stack.enter_context(mock.patch.object(settings, "voucher_bucket", ""))
        stack.enter_context(mock.patch.object(settings, "tally_push_url", ""))
        stack.enter_context(mock.patch.object(settings, "valence_api_key", "vk_doctor"))
        yield url


def test_doctor_reports_every_check_and_exits_zero_when_healthy(healthy, capsys):
    code = main(["doctor", "--database-url", healthy])

    output = capsys.readouterr().out
    assert code == 0, output
    for name in CHECK_NAMES:
        assert name in output
    assert all(f"PASS {name}" in output for name in CHECK_NAMES)


def test_doctor_json_output_has_one_result_per_check(healthy, capsys):
    code = main(["doctor", "--database-url", healthy, "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert {item["name"] for item in payload} == CHECK_NAMES
    assert all(item["ok"] for item in payload)


def test_doctor_exits_nonzero_when_a_check_fails(healthy, capsys):
    with mock.patch.object(settings, "model_provider", "unknown-provider"):
        code = main(["doctor", "--database-url", healthy])

    output = capsys.readouterr().out
    assert code == 1
    assert "FAIL model provider" in output


def test_doctor_reports_a_database_that_cannot_be_reached(tmp_path, capsys):
    url = _sqlite_url(tmp_path / "missing-parent")

    code = main(["doctor", "--database-url", url])

    assert code == 1
    assert "FAIL database" in capsys.readouterr().out


def test_doctor_reports_pending_migrations(tmp_path, capsys):
    url = _sqlite_url(tmp_path)
    asyncio.run(SqlOrderStore(url).initialize())

    with (
        mock.patch.object(settings, "model_provider", "fake"),
        mock.patch.object(settings, "voucher_dir", str(tmp_path)),
        mock.patch.object(settings, "tally_push_url", "http://tally.local/import"),
    ):
        code = main(["doctor", "--database-url", url])

    assert code == 1
    assert "FAIL migrations" in capsys.readouterr().out


def test_doctor_never_prints_secret_values(healthy, capsys):
    secrets = {
        "meta_access_token": "meta-token-doctor-secret",
        "meta_app_secret": "app-secret-doctor-secret",
        "web_passcode": "passcode-doctor-secret",
        "web_passcode_salt": "salt-doctor-secret",
        "setup_token": "setup-doctor-secret",
    }
    with ExitStack() as stack:
        for name, value in secrets.items():
            stack.enter_context(mock.patch.object(settings, name, value))
        stack.enter_context(
            mock.patch.object(
                settings,
                "database_url",
                "postgresql+asyncpg://valence:hunter2-db-secret@db.local/valence",
            )
        )
        code = main(["doctor", "--database-url", healthy])

    output = capsys.readouterr().out
    assert code == 0
    for secret in [*secrets.values(), "hunter2-db-secret"]:
        assert secret not in output
    assert "***" in output or "redacted" in output.lower()


def test_doctor_reports_an_unsupported_url_without_a_traceback(capsys):
    url = "postgresql+psycopg://valence:hunter2-doctor-secret@db.local/valence"

    code = main(["doctor", "--database-url", url])

    output = capsys.readouterr().out
    assert code == 1
    assert "FAIL configuration" in output
    assert "FAIL database" in output
    assert "hunter2-doctor-secret" not in output
    assert "Traceback" not in capsys.readouterr().err


def test_doctor_help_documents_database_and_json_options(capsys):
    with pytest.raises(SystemExit) as error:
        main(["doctor", "--help"])

    assert error.value.code == 0
    help_text = capsys.readouterr().out
    assert "--database-url" in help_text
    assert "--json" in help_text


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_doctor_connects_and_checks_migrations_on_both_sql_backends(
    tmp_path, capsys, backend
):
    url = _sqlite_url(tmp_path)
    if backend == "postgres":
        postgres_url = os.environ.get("TEST_POSTGRES_URL", "")
        if not postgres_url:
            pytest.skip("set TEST_POSTGRES_URL to run Postgres conformance")
        url = postgres_url
    _migrate(url)

    with (
        mock.patch.object(settings, "model_provider", "fake"),
        mock.patch.object(settings, "voucher_dir", str(tmp_path)),
        mock.patch.object(settings, "tally_push_url", "http://tally.local/import"),
        mock.patch.dict(os.environ, {"META_ACCESS_TOKEN": "fake-meta-token"}),
    ):
        code = main(["doctor", "--database-url", url])

    output = capsys.readouterr().out
    assert code == 0, output
    assert "PASS database" in output
    assert "PASS migrations" in output
