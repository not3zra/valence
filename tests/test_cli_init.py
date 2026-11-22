"""``valence init`` bootstraps one empty Instance (issue #57, ADR-0009)."""

from __future__ import annotations

import asyncio

import pytest

from src.auth import AuthService, Role
from src.cli import main
from src.sql_store import SqlAuthStore


def _url(tmp_path):
    return f"sqlite+aiosqlite:///{tmp_path / 'valence.db'}"


def test_init_creates_schema_instance_and_first_admin_key(tmp_path, capsys):
    url = _url(tmp_path)

    code = main(
        [
            "init",
            "--database-url",
            url,
            "--business-name",
            "Acme Chemicals",
            "--admin-name",
            "ops-admin",
        ]
    )

    assert code == 0
    output = capsys.readouterr().out
    assert "vk_" in output
    assert "Acme Chemicals" in output
    assert "shown once" in output
    service = AuthService(SqlAuthStore(url))
    profile = asyncio.run(service.get_instance())
    assert profile is not None and profile.business_name == "Acme Chemicals"
    keys = asyncio.run(service.list_keys())
    assert len(keys) == 1
    assert keys[0].role is Role.ADMIN and keys[0].name == "ops-admin"


def test_init_is_idempotent_and_never_issues_a_second_admin(tmp_path, capsys):
    url = _url(tmp_path)
    args = ["init", "--database-url", url, "--business-name", "Acme"]

    assert main(args) == 0
    first_output = capsys.readouterr().out
    assert main(args) == 0
    second_output = capsys.readouterr().out

    assert "vk_" in first_output
    assert "vk_" not in second_output
    assert "already initialized" in second_output
    service = AuthService(SqlAuthStore(url))
    keys = asyncio.run(service.list_keys())
    assert len(keys) == 1


def test_init_rejects_unsupported_stores(tmp_path):
    with pytest.raises(SystemExit):
        main(
            [
                "init",
                "--database-url",
                "firestore://projects/demo",
                "--business-name",
                "Acme",
            ]
        )
