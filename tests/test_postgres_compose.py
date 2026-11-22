"""Postgres and Compose deployment profile (issue #56).

The seams are the ones the spec pre-agrees: the Alembic migration entrypoint,
the ``OrderStore`` protocol shared by SQLite and Postgres, the runtime
composition root, and the Compose deployment contract. Postgres conformance
tests run when ``TEST_POSTGRES_URL`` points at a disposable database; SQLite
always runs.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from src import seed_data
from src.config import settings
from src.loading import load_loading_list
from src.model_provider import FakeModelProvider
from src.orders import EVENT_ORDER_CREATED, Order, OrderEvent, OrderStatus
from src.runtime import build_runtime
from src.sql_store import SqlOrderStore
from src.voucher import prepare_voucher

from .fakes import ToolCallingLlm, approved_order_id

ROOT = Path(__file__).resolve().parents[1]
POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL", "")
APPLICATION_TABLES = {
    "valence_orders",
    "valence_order_events",
    "valence_pending_approvals",
    "valence_catalog",
}
requires_postgres = pytest.mark.skipif(
    not POSTGRES_URL, reason="set TEST_POSTGRES_URL to run Postgres conformance"
)


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'migrated.db'}"


def _alembic(*args: str, url: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": url},
        text=True,
        capture_output=True,
        check=False,
    )


async def _table_names(url: str) -> set[str]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            names = await connection.run_sync(
                lambda c: inspect(c).get_table_names()
            )
            return set(names)
    finally:
        await engine.dispose()


@pytest.fixture(params=["sqlite", "postgres"])
def store_url(request, tmp_path):
    if request.param == "postgres":
        if not POSTGRES_URL:
            pytest.skip("set TEST_POSTGRES_URL to run Postgres conformance")
        return POSTGRES_URL
    return _sqlite_url(tmp_path)


def test_initial_migration_creates_application_schema_on_sqlite(tmp_path):
    url = _sqlite_url(tmp_path)

    result = _alembic("upgrade", "head", url=url)

    assert result.returncode == 0, result.stderr
    assert APPLICATION_TABLES <= asyncio.run(_table_names(url))
    assert _alembic("upgrade", "head", url=url).returncode == 0


@requires_postgres
def test_initial_migration_creates_application_schema_on_postgres():
    result = _alembic("upgrade", "head", url=POSTGRES_URL)

    assert result.returncode == 0, result.stderr
    assert APPLICATION_TABLES <= asyncio.run(_table_names(POSTGRES_URL))
    assert _alembic("upgrade", "head", url=POSTGRES_URL).returncode == 0


async def test_store_conformance_across_sql_backends(store_url):
    assert _alembic("upgrade", "head", url=store_url).returncode == 0
    store = SqlOrderStore(store_url)

    assert await store.get_config() == seed_data.CONFIG
    assert [c.id for c in await store.get_customers()] == [
        c.id for c in seed_data.CUSTOMERS
    ]
    assert [p.id for p in await store.get_products()] == [
        p.id for p in seed_data.PRODUCTS
    ]
    assert [r.id for r in await store.get_routes()] == [r.id for r in seed_data.ROUTES]

    order = Order(
        phone=f"+91{uuid.uuid4().hex[:10]}",
        items=[],
        customer="ChemFab Industries",
        customer_id="c_chemfab",
        delivery_location="Peenya Industrial Area",
        confidence=0.9,
        order_id=f"ord_{uuid.uuid4().hex}",
        status=OrderStatus.APPROVED,
    )
    await store.create_order(order)
    await store.append_order_event(
        OrderEvent(
            order_id=order.order_id,
            event_type=EVENT_ORDER_CREATED,
            payload={"source": "sql-conformance"},
        )
    )
    approver = f"+91{uuid.uuid4().hex[:10]}"
    await store.set_pending_approval(approver, order.order_id)

    restarted = SqlOrderStore(store_url)
    assert await restarted.get_order(order.order_id) == order
    assert order in await restarted.list_all_orders()
    assert order in await restarted.list_approved_orders()
    assert order in await restarted.list_orders(phone=order.phone)
    events = await restarted.list_order_events(order.order_id)
    assert len(events) == 1
    assert events[0].event_type == EVENT_ORDER_CREATED
    assert events[0].payload == {"source": "sql-conformance"}
    assert await restarted.get_pending_approval(approver) == order.order_id
    assert await restarted.get_pending_approvals_list(approver) == [order.order_id]


@requires_postgres
def test_order_flow_on_postgres_uses_the_composed_runtime(tmp_path):
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(settings, "order_store", "postgres"))
        stack.enter_context(mock.patch.object(settings, "session_service", "postgres"))
        stack.enter_context(mock.patch.object(settings, "database_url", POSTGRES_URL))
        stack.enter_context(
            mock.patch.object(settings, "voucher_dir", str(tmp_path / "vouchers"))
        )
        stack.enter_context(mock.patch.object(settings, "model_provider", "fake"))
        runtime = build_runtime(
            model_provider=FakeModelProvider(model=ToolCallingLlm())
        )
    try:
        sender = f"+91{uuid.uuid4().hex[:10]}"
        assert runtime.run_turn(sender_id=sender, message="one drum") == (
            "Order committed."
        )
        orders = asyncio.run(runtime.store.list_all_orders())
        assert any(order.phone == sender for order in orders)
        order_id = asyncio.run(approved_order_id(runtime.store))
        voucher = asyncio.run(
            prepare_voucher(runtime.store, runtime.voucher_storage, order_id)
        )
        assert (tmp_path / "vouchers" / f"{voucher.voucher_id}.xml").exists()
        loading = asyncio.run(load_loading_list(runtime.store))
        entries = [entry for section in loading.sections for entry in section.entries]
        entries += loading.unrouted + loading.late
        assert order_id in {entry.order_id for entry in entries}
    finally:
        runtime.stop()


def test_compose_waits_for_a_healthy_postgres_and_exposes_the_healthcheck():
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not available")
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(ROOT / "docker-compose.yml"),
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env={**os.environ, "POSTGRES_PASSWORD": "compose-config-test"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]

    assert {"app", "postgres"} <= set(services)
    assert services["app"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert services["postgres"]["healthcheck"]["test"]
    assert services["app"]["healthcheck"]["test"]
    assert "alembic upgrade head" in (ROOT / "scripts/docker-entrypoint.sh").read_text()
