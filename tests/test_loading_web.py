"""The dispatch-facing Loading List web view (issue #9).

The page renders live approved orders through the same ``load_loading_list``
path as the ADK tool, behind the configured passcode from the store config.
The retired pre-v1 Cutoff endpoint is covered as gone by the retirement tests;
the versioned trigger is ``POST /api/v1/loading-lists``.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from google.adk.sessions import InMemorySessionService

from src.agent import build_agent
from src.orders import Order, OrderItem, OrderStatus
from src.store import InMemoryOrderStore
from src.web import create_app

from .fakes import FakeEchoLlm

PASSCODE = "test-passcode"
PASSCODE_SALT = "test-salt"
ORIGIN = "http://testserver"
# Fixed well-before-cutoff stamps: the tests must not depend on the wall clock
# (a late-afternoon run would reclassify the fixtures as late add-ons).
NOW = "2026-08-14T08:00:00+00:00"


def _approved_order(order_id: str, *, location_id: str = "dl_peenya") -> Order:
    return Order(
        order_id=order_id,
        phone="+919812345001",
        customer="ChemFab Industries",
        customer_id="c_chemfab",
        delivery_location="Peenya Industrial Area",
        delivery_location_id=location_id,
        items=[OrderItem(product="Sulfuric Acid", quantity=2000, unit="kg")],
        status=OrderStatus.APPROVED,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture
def store():
    store = InMemoryOrderStore()
    store.orders.append(_approved_order("ord_west", location_id="dl_peenya"))
    store.orders.append(_approved_order("ord_east", location_id="dl_whitefield"))
    return store


@pytest.fixture
def client(store):
    app = create_app(
        agent=build_agent(model=FakeEchoLlm()),
        session_service=InMemorySessionService(),
        store=store,
        web_passcode=PASSCODE,
        web_passcode_salt=PASSCODE_SALT,
        web_cookie_secure=False,
    )
    return TestClient(app)


def _login(client) -> None:
    response = client.post(
        "/loading/login",
        data={"passcode": PASSCODE},
        headers={"Origin": ORIGIN},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _post(client, url, **kwargs):
    return client.post(url, headers={"Origin": ORIGIN}, **kwargs)


def test_loading_page_is_gated_behind_the_passcode(client):
    response = client.get("/loading")
    assert response.status_code == 200
    assert "passcode" in response.text.lower()


def test_loading_page_renders_approved_orders_after_login(client):
    _login(client)
    response = client.get("/loading")
    assert response.status_code == 200
    assert "ord_west" in response.text
    assert "ord_east" in response.text
    assert "Bengaluru West" in response.text
    assert "Bengaluru East" in response.text


def test_loading_page_rejects_a_malformed_delivery_day(client):
    _login(client)
    response = client.get("/loading?day=not-a-date")
    assert response.status_code == 400


def test_loading_login_sets_a_secure_cookie_when_over_https(store):
    app = create_app(
        agent=build_agent(model=FakeEchoLlm()),
        session_service=InMemorySessionService(),
        store=store,
        web_passcode=PASSCODE,
        web_passcode_salt=PASSCODE_SALT,
        web_cookie_secure=True,
    )
    client = TestClient(app)
    response = client.post(
        "/loading/login",
        data={"passcode": PASSCODE},
        headers={"Origin": ORIGIN},
        follow_redirects=False,
    )
    assert response.status_code == 303
    cookie = next(
        c for c in response.cookies.jar if c.name == "valence_loading"
    )
    assert cookie.secure is True


def test_loading_page_is_printable_and_price_free(client):
    _login(client)
    response = client.get("/loading")
    assert response.status_code == 200
    assert "@media print" in response.text
    assert "INR" not in response.text


def test_loading_login_rejects_wrong_passcode(client):
    response = _post(client, "/loading/login", data={"passcode": "wrong"})
    assert response.status_code == 200
    assert "Incorrect passcode" in response.text


def test_loading_logout_revokes_access(client):
    _login(client)
    assert client.get("/loading").status_code == 200
    response = _post(client, "/loading/logout", follow_redirects=False)
    assert response.status_code == 303
    assert "passcode" in client.get("/loading").text.lower()


def test_dispatch_action_is_gated(client):
    response = _post(client, "/loading/orders/ord_west/dispatch")
    assert response.status_code == 401


def test_dispatch_action_marks_order_dispatched(client, store):
    _login(client)
    response = _post(
        client, "/loading/orders/ord_west/dispatch", follow_redirects=False
    )
    assert response.status_code == 303
    order = asyncio.run(store.get_order("ord_west"))
    assert order.status is OrderStatus.DISPATCHED


def test_dispatch_action_missing_order_is_404(client):
    _login(client)
    response = _post(client, "/loading/orders/ord_missing/dispatch")
    assert response.status_code == 404


def test_dispatch_action_is_recorded_as_an_order_event(client, store):
    _login(client)
    _post(client, "/loading/orders/ord_east/dispatch")
    events = store.events
    assert any(e.event_type == "order_dispatched" for e in events)


def test_loading_view_is_closed_when_passcode_unconfigured(store):
    app = create_app(
        agent=build_agent(model=FakeEchoLlm()),
        session_service=InMemorySessionService(),
        store=store,
    )
    response = TestClient(app).get("/loading")
    assert response.status_code == 503


def test_loading_login_is_closed_when_passcode_unconfigured(store):
    app = create_app(
        agent=build_agent(model=FakeEchoLlm()),
        session_service=InMemorySessionService(),
        store=store,
    )
    response = TestClient(app).post(
        "/loading/login",
        data={"passcode": ""},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 503
