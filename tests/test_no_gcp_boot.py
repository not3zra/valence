"""No-GCP local boot (issue #55): SQLite, durable sessions, filesystem vouchers.

The default install must reach a first order without GCP: configuration selects
a SQLite order store, a durable agent session service on the same database, and
a filesystem voucher store. These tests exercise the seams the spec pre-agrees
— the order-store protocol, the voucher-store port, and the runtime composition
root — including a full stop/restart across two runtime instances.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from src import seed_data
from src.agent import build_session_service
from src.config import settings
from src.model_provider import FakeModelProvider
from src.orders import EVENT_ORDER_CREATED, Order, OrderEvent, OrderStatus
from src.runtime import build_runtime
from src.sql_store import SqlOrderStore
from src.voucher import FilesystemVoucherStore, prepare_voucher
from src.web import create_app

from .fakes import ToolCallingLlm, approved_order_id
from .test_optional_gcp import _BLOCK_GCP

ROOT = Path(__file__).resolve().parents[1]
PHONE = "+919812345001"


async def test_filesystem_voucher_store_round_trips_xml(tmp_path):
    storage = FilesystemVoucherStore(tmp_path)

    await storage.write("voucher_ord_abc", "<ENVELOPE/>")

    assert (tmp_path / "voucher_ord_abc.xml").read_text() == "<ENVELOPE/>"
    assert await storage.read("voucher_ord_abc") == "<ENVELOPE/>"


@pytest.mark.parametrize("unsafe", ["../escape", "a/b", "..", "", "/abs"])
async def test_filesystem_voucher_store_rejects_unsafe_ids(tmp_path, unsafe):
    storage = FilesystemVoucherStore(tmp_path)

    with pytest.raises(ValueError):
        await storage.write(unsafe, "<x/>")

    assert list(tmp_path.iterdir()) == []


def _sqlite_url(tmp_path, name: str = "valence.db") -> str:
    return f"sqlite+aiosqlite:///{tmp_path / name}"


async def test_fresh_sqlite_store_serves_the_sample_catalog(tmp_path):
    store = SqlOrderStore(_sqlite_url(tmp_path))

    assert await store.get_config() == seed_data.CONFIG
    assert [c.id for c in await store.get_customers()] == [
        c.id for c in seed_data.CUSTOMERS
    ]
    assert [p.id for p in await store.get_products()] == [
        p.id for p in seed_data.PRODUCTS
    ]
    assert [d.id for d in await store.get_delivery_locations()] == [
        d.id for d in seed_data.DELIVERY_LOCATIONS
    ]
    assert [a.id for a in await store.get_approvers()] == [
        a.id for a in seed_data.APPROVERS
    ]
    assert [r.id for r in await store.get_routes()] == [
        r.id for r in seed_data.ROUTES
    ]


async def test_sqlite_store_persists_orders_events_and_approvals_across_restart(
    tmp_path,
):
    url = _sqlite_url(tmp_path)
    store = SqlOrderStore(url)
    order = Order(
        phone="+919812345001",
        items=[],
        customer="ChemFab Industries",
        customer_id="c_chemfab",
        delivery_location="Peenya Industrial Area",
        confidence=0.9,
        order_id="ord_abc123",
        status=OrderStatus.APPROVED,
    )
    await store.create_order(order)
    await store.append_order_event(
        OrderEvent(
            order_id=order.order_id,
            event_type=EVENT_ORDER_CREATED,
            payload={"source": "local-boot"},
        )
    )
    await store.set_pending_approval("+919900000001", order.order_id)

    restarted = SqlOrderStore(url)

    assert await restarted.get_order(order.order_id) == order
    assert await restarted.list_orders(phone=order.phone) == [order]
    assert await restarted.list_all_orders() == [order]
    assert await restarted.list_approved_orders() == [order]
    events = await restarted.list_order_events(order.order_id)
    assert len(events) == 1
    assert events[0].event_type == EVENT_ORDER_CREATED
    assert events[0].payload == {"source": "local-boot"}
    assert await restarted.get_pending_approval("+919900000001") == order.order_id
    assert await restarted.get_pending_approvals_list("+919900000001") == [
        order.order_id
    ]


def _configured(tmp_path, *, url: str | None = None, voucher_dir: Path | None = None):
    stack = ExitStack()
    stack.enter_context(mock.patch.object(settings, "order_store", "sqlite"))
    stack.enter_context(mock.patch.object(settings, "session_service", "sqlite"))
    stack.enter_context(
        mock.patch.object(settings, "database_url", url or _sqlite_url(tmp_path))
    )
    stack.enter_context(
        mock.patch.object(
            settings,
            "voucher_dir",
            str(voucher_dir or tmp_path / "vouchers"),
        )
    )
    stack.enter_context(mock.patch.object(settings, "model_provider", "fake"))
    return stack


def test_build_runtime_selects_sqlite_and_filesystem_from_configuration(tmp_path):
    with _configured(tmp_path):
        runtime = build_runtime(
            model_provider=FakeModelProvider(model=ToolCallingLlm())
        )
    try:
        assert runtime.run_turn(sender_id=PHONE, message="one drum") == (
            "Order committed."
        )
        assert len(asyncio.run(runtime.store.list_all_orders())) == 1
    finally:
        runtime.stop()


def test_order_and_session_survive_runtime_restart(tmp_path):
    url = _sqlite_url(tmp_path)
    with _configured(tmp_path, url=url):
        first = build_runtime(
            model_provider=FakeModelProvider(model=ToolCallingLlm())
        )
        try:
            assert first.run_turn(sender_id=PHONE, message="first order") == (
                "Order committed."
            )
            orders = asyncio.run(first.store.list_all_orders())
            assert len(orders) == 1
            order_id = orders[0].order_id
            assert asyncio.run(first.store.list_order_events(order_id))
        finally:
            first.stop()

        second = build_runtime()
        try:
            restored = asyncio.run(second.store.list_all_orders())
            assert [order.order_id for order in restored] == [order_id]
            service = build_session_service()
            sessions = asyncio.run(
                service.list_sessions(app_name=settings.app_name, user_id=PHONE)
            )
            assert [session.id for session in sessions.sessions] == [PHONE]
            session = asyncio.run(
                service.get_session(
                    app_name=settings.app_name, user_id=PHONE, session_id=PHONE
                )
            )
            assert session is not None and session.events
            assert second.run_turn(sender_id=PHONE, message="still here") == (
                "Echo: still here"
            )
        finally:
            second.stop()


def test_filesystem_voucher_is_downloadable_after_restart(tmp_path):
    voucher_dir = tmp_path / "vouchers"
    with _configured(tmp_path, voucher_dir=voucher_dir):
        first = build_runtime()
        try:
            order_id = asyncio.run(approved_order_id(first.store))
            voucher = asyncio.run(
                prepare_voucher(first.store, first.voucher_storage, order_id)
            )
            assert (voucher_dir / f"{voucher.voucher_id}.xml").exists()
        finally:
            first.stop()

        second = build_runtime()
        try:
            client = TestClient(
                create_app(
                    runtime=second,
                    web_passcode="pw",
                    web_passcode_salt="salt",
                    web_cookie_secure=False,
                )
            )
            client.post(
                "/review/login",
                data={"passcode": "pw"},
                headers={"Origin": "http://testserver"},
            )
            response = client.get(
                f"/review/orders/{order_id}/voucher",
                headers={"Origin": "http://testserver"},
            )
            assert response.status_code == 200
            assert "<ENVELOPE>" in response.text
        finally:
            second.stop()


def test_configuration_only_sqlite_boot_imports_no_gcp(tmp_path):
    env = {
        "ORDER_STORE": "sqlite",
        "SESSION_SERVICE": "sqlite",
        "DATABASE_URL": _sqlite_url(tmp_path),
        "VOUCHER_DIR": str(tmp_path / "vouchers"),
        "MODEL_PROVIDER": "fake",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _BLOCK_GCP
            + "import os\nos.environ.update("
            + repr(env)
            + ")\n"
            + "import src.main\nfrom src.runtime import build_runtime\n"
            + "runtime = build_runtime()\n"
            + "assert runtime.run_turn(sender_id='+9000000001', message='ping') "
            + "== 'Echo: ping'\n"
            + "assert 'google.cloud' not in sys.modules\n"
            + "runtime.stop()\n",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
