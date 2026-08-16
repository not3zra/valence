"""The voucher web actions (issue #8): prepare, download, mark billed.

Exercised end-to-end against the HTTP surface: the approved order's voucher
card prepares and stores a voucher through the same seam as the ADK tool, the
stored XML is downloadable for manual Tally import, and a prepared voucher can
be marked billed. All routes are passcode-gated like the rest of the review
view.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from google.adk.sessions import InMemorySessionService

from src.agent import build_agent
from src.orders import OrderStatus
from src.store import InMemoryOrderStore
from src.voucher import InMemoryVoucherStore
from src.web import create_app

from .fakes import FakeEchoLlm, approved_order_id

PASSCODE = "valence-demo"
PASSCODE_SALT = "test-salt"
ORIGIN = "http://testserver"


@pytest.fixture
def store():
    return InMemoryOrderStore()


@pytest.fixture
def storage():
    return InMemoryVoucherStore()


@pytest.fixture
def client(store, storage):
    app = create_app(
        agent=build_agent(model=FakeEchoLlm(), store=store),
        session_service=InMemorySessionService(),
        store=store,
        web_passcode=PASSCODE,
        web_passcode_salt=PASSCODE_SALT,
        web_cookie_secure=False,
        voucher_storage=storage,
    )
    return TestClient(app)


def _login(client):
    return client.post(
        "/review/login",
        data={"passcode": PASSCODE},
        headers={"Origin": ORIGIN},
    )


def _post(client, url, **kwargs):
    return client.post(url, headers={"Origin": ORIGIN}, **kwargs)


async def test_prepare_voucher_button_stores_and_redirects(client, store, storage):
    order_id = await approved_order_id(store)
    _login(client)

    response = _post(
        client,
        f"/review/orders/{order_id}/prepare-voucher",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert f"/review/orders/{order_id}?notice=" in response.headers["location"]
    order = store.orders[-1]
    assert order.voucher_id == f"voucher_{order_id}"
    assert order.voucher_id in storage.blobs
    assert any(
        e.event_type == "voucher_ready" and e.order_id == order_id for e in store.events
    )


async def test_prepare_voucher_requires_login(client, store, storage):
    order_id = await approved_order_id(store)

    response = client.post(f"/review/orders/{order_id}/prepare-voucher")

    assert response.status_code == 401
    assert store.orders[-1].voucher_id is None
    assert storage.blobs == {}


async def test_prepare_voucher_rejects_cross_origin_even_when_logged_in(
    client, store, storage
):
    order_id = await approved_order_id(store)
    _login(client)

    response = client.post(f"/review/orders/{order_id}/prepare-voucher")

    assert response.status_code == 403
    assert store.orders[-1].voucher_id is None
    assert storage.blobs == {}


async def test_unmapped_master_surfaces_error_and_writes_nothing(
    client, store, storage
):
    order_id = await approved_order_id(store)
    _login(client)
    store.products = []

    response = _post(
        client,
        f"/review/orders/{order_id}/prepare-voucher",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "not mapped" in response.text
    assert storage.blobs == {}
    assert store.orders[-1].voucher_id is None


async def test_voucher_download_returns_the_stored_xml(client, store, storage):
    order_id = await approved_order_id(store)
    _login(client)
    _post(client, f"/review/orders/{order_id}/prepare-voucher")

    response = client.get(f"/review/orders/{order_id}/voucher")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert f"voucher_{order_id}" in response.headers["content-disposition"]
    assert storage.blobs[f"voucher_{order_id}"] == response.text
    assert "SULFURIC ACID 98%" in response.text


async def test_voucher_download_requires_login(client, store, storage):
    order_id = await approved_order_id(store)
    _login(client)
    _post(client, f"/review/orders/{order_id}/prepare-voucher")
    client.cookies.clear()

    response = client.get(f"/review/orders/{order_id}/voucher")

    assert response.status_code == 401


async def test_voucher_download_404_when_not_prepared(client, store):
    order_id = await approved_order_id(store)
    _login(client)

    response = client.get(f"/review/orders/{order_id}/voucher")

    assert response.status_code == 404


async def test_mark_billed_transitions_and_records_event(client, store, storage):
    order_id = await approved_order_id(store)
    _login(client)
    _post(client, f"/review/orders/{order_id}/prepare-voucher")

    response = _post(
        client,
        f"/review/orders/{order_id}/billed",
        follow_redirects=False,
    )

    assert response.status_code == 303
    order = store.orders[-1]
    assert order.status is OrderStatus.BILLED
    assert any(
        e.event_type == "order_billed"
        and e.order_id == order_id
        and e.payload.get("voucher_id") == f"voucher_{order_id}"
        for e in store.events
    )


async def test_mark_billed_without_voucher_conflicts(client, store):
    order_id = await approved_order_id(store)
    _login(client)

    response = _post(client, f"/review/orders/{order_id}/billed")

    assert response.status_code == 409
    assert store.orders[-1].status is OrderStatus.APPROVED


async def test_mark_billed_requires_login(client, store, storage):
    order_id = await approved_order_id(store)
    _login(client)
    _post(client, f"/review/orders/{order_id}/prepare-voucher")
    client.cookies.clear()

    response = client.post(f"/review/orders/{order_id}/billed")

    assert response.status_code == 401
    assert store.orders[-1].status is OrderStatus.APPROVED


async def test_order_detail_shows_voucher_card_and_actions(client, store, storage):
    order_id = await approved_order_id(store)
    _login(client)

    pending = client.get(f"/review/orders/{order_id}")
    assert "Tally voucher" in pending.text
    assert "Prepare voucher" in pending.text
    assert "Download voucher XML" not in pending.text

    _post(client, f"/review/orders/{order_id}/prepare-voucher")

    ready = client.get(f"/review/orders/{order_id}")
    assert "Download voucher XML" in ready.text
    assert "Mark billed" in ready.text
    assert "Prepare voucher" not in ready.text


def test_voucher_card_hidden_without_login(client, store, storage):
    order_id = "ord_missing"
    response = client.get(f"/review/orders/{order_id}")
    assert response.status_code == 401
