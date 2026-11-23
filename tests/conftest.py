"""Shared pytest fixtures for the suite."""

from __future__ import annotations

import asyncio
import os
import re

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from tests.fake_tally import fake_tally

__all__ = ["fake_tally"]


async def _recreate_postgres_database(url: str) -> None:
    """Recreate the disposable Postgres database named by ``url``.

    Every Postgres test points at the same database, so each one gets a clean
    schema and an empty order set instead of inheriting another test's data.
    """
    parsed = make_url(url)
    name = parsed.database
    if not name or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise RuntimeError(f"refusing to recreate database with unsafe name: {name!r}")
    if name.lower() in {"postgres", "template0", "template1"}:
        raise RuntimeError(f"refusing to recreate protected database: {name!r}")
    connection = await asyncpg.connect(
        user=parsed.username,
        password=parsed.password,
        host=parsed.host,
        port=parsed.port,
        database="postgres",
    )
    try:
        await connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await connection.execute(f'CREATE DATABASE "{name}"')
    finally:
        await connection.close()


@pytest.fixture(autouse=True)
def fresh_postgres_database(request: pytest.FixtureRequest) -> None:
    """Give each Postgres-marked test a fresh, empty database."""
    url = os.environ.get("TEST_POSTGRES_URL", "")
    if not url or not any(m.name == "postgres" for m in request.node.iter_markers()):
        return
    asyncio.run(_recreate_postgres_database(url))


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark the Postgres case of backend-parametrised tests for CI selection.

    Both ``store_url`` and ``backend`` fixtures take a ``postgres`` parameter
    value; only that case needs the disposable database. The SQLite cases stay
    unmarked and are covered by the ordinary suite.
    """
    for item in items:
        callspec = getattr(item, "callspec", None)
        params = callspec.params if callspec else {}
        if "postgres" in params.values():
            item.add_marker(pytest.mark.postgres)
