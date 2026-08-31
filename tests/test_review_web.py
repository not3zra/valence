"""The review web view (issue #6): the passcode-gated human interface.

Exercised end-to-end against the HTTP surface with a fake LLM and the in-memory
store: the escalation queue with reason badges, an order detail with its Order
Event timeline, search, the live stat bar, and web approve/reject going through
the same Order Processing Core as WhatsApp.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from google.adk.sessions import InMemorySessionService

from src.agent import build_agent
from src.core import OrderProcessingCore
from src.orders import Order, OrderItem, OrderStatus
from src.review import PASSCODE_COOKIE
from src.store import InMemoryOrderStore
from src.web import _passcode_digest, create_app

from .fakes import FakeEchoLlm

PASSCODE = "valence-demo"
PASSCODE_SALT = "test-salt"
ORIGIN = "http://testserver"


@pytest.fixture
def store():
    return InMemoryOrderStore()


@pytest.fixture
def client(store):
    app = create_app(
        agent=build_agent(model=FakeEchoLlm(), store=store),
        session_service=InMemorySessionService(),
        store=store,
        web_passcode=PASSCODE,
        web_passcode_salt=PASSCODE_SALT,
        web_cookie_secure=False,
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


async def _escalated_order(store) -> str:
    core = OrderProcessingCore(store)
    decision = await core.process(
        Order(
            phone="+919999999999",
            customer=None,
            items=[OrderItem(product="sulfuric acid", quantity=2000, unit="kg")],
            delivery_location=None,
            confidence=0.9,
            source_channel="phone",
        )
    )
    return decision.order_id


def test_review_requires_login(client):
    response = client.get("/review")
    assert response.status_code == 200
    assert "Passcode" in response.text


def test_login_with_wrong_passcode_shows_error(client):
    response = _post(client, "/review/login", data={"passcode": "nope"})
    assert response.status_code == 200
    assert "Incorrect passcode" in response.text
    assert client.cookies.get(PASSCODE_COOKIE) is None


def test_login_with_correct_passcode_sets_cookie(client):
    response = _post(
        client,
        "/review/login",
        data={"passcode": PASSCODE},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert client.cookies.get(PASSCODE_COOKIE) == _passcode_digest(
        PASSCODE, PASSCODE_SALT
    )


async def test_escalated_order_appears_in_queue_with_reason_badge(client, store):
    await _escalated_order(store)
    _login(client)

    response = client.get("/review")

    assert response.status_code == 200
    assert "Unknown customer" in response.text


async def test_clean_order_does_not_appear_in_queue(client, store):
    core = OrderProcessingCore(store)
    await core.process(
        Order(
            phone="+919812345001",
            customer="ChemFab Industries",
            items=[OrderItem(product="sulfuric acid", quantity=2000, unit="kg")],
            delivery_location="Peenya Industrial Area",
            confidence=0.9,
        )
    )
    _login(client)

    response = client.get("/review")

    assert response.status_code == 200
    assert "No orders found" in response.text


async def test_order_detail_shows_timeline_and_fields(client, store):
    order_id = await _escalated_order(store)
    _login(client)

    response = client.get(f"/review/orders/{order_id}")

    assert response.status_code == 200
    assert order_id in response.text
    assert "order_escalated" in response.text
    assert "sulfuric acid" in response.text


async def test_order_detail_is_unauthorized_without_login(client, store):
    order_id = await _escalated_order(store)

    response = client.get(f"/review/orders/{order_id}")

    assert response.status_code == 401


async def test_web_approve_transitions_order_through_core(client, store):
    order_id = await _escalated_order(store)
    _login(client)

    response = _post(
        client, f"/review/orders/{order_id}/approve", follow_redirects=False
    )

    assert response.status_code == 303
    order = store.orders[-1]
    assert order.status is OrderStatus.APPROVED
    assert any(
        e.event_type == "order_approved" and e.order_id == order_id
        for e in store.events
    )


async def test_web_reject_transitions_order_to_terminal_rejected(client, store):
    order_id = await _escalated_order(store)
    _login(client)

    response = _post(
        client, f"/review/orders/{order_id}/reject", follow_redirects=False
    )

    assert response.status_code == 303
    order = store.orders[-1]
    assert order.status is OrderStatus.REJECTED
    assert any(
        e.event_type == "order_rejected" and e.order_id == order_id
        for e in store.events
    )


async def test_web_approve_requires_login(client, store):
    order_id = await _escalated_order(store)

    response = client.post(f"/review/orders/{order_id}/approve")

    assert response.status_code == 401
    assert store.orders[-1].status is OrderStatus.PENDING_REVIEW


async def test_web_decide_rejects_cross_origin_even_when_logged_in(client, store):
    order_id = await _escalated_order(store)
    _login(client)

    response = client.post(f"/review/orders/{order_id}/approve")

    assert response.status_code == 403
    assert store.orders[-1].status is OrderStatus.PENDING_REVIEW


async def test_search_filters_orders(client, store):
    await _escalated_order(store)
    _login(client)

    found = client.get("/review/orders", params={"q": "999999"})
    assert "No orders found" not in found.text

    missed = client.get("/review/orders", params={"q": "zzznothing"})
    assert "No orders found" in missed.text


async def test_search_matches_order_event_payloads(client, store):
    await _escalated_order(store)
    _login(client)

    response = client.get("/review/orders", params={"q": "missing_field"})
    assert "No orders found" not in response.text


async def test_search_echoes_query_into_the_box(client, store):
    await _escalated_order(store)
    _login(client)

    response = client.get("/review/orders", params={"q": "missing_field"})

    assert "value='missing_field'" in response.text


async def test_stats_returns_live_counts(client, store):
    await _escalated_order(store)
    _login(client)

    response = client.get("/review/stats")

    assert response.status_code == 200
    body = response.json()
    assert body["pending_escalations"] == 1
    assert body["approved_today"] == 0


def test_review_stats_requires_login(client):
    response = client.get("/review/stats")
    assert response.status_code == 401


def test_logout_clears_session(client):
    _login(client)
    assert client.cookies.get(PASSCODE_COOKIE) is not None

    _post(client, "/review/logout")

    response = client.get("/review")
    assert "Passcode" in response.text


async def test_edit_page_requires_login(client, store):
    order_id = await _escalated_order(store)
    response = client.get(f"/review/orders/{order_id}/edit")
    assert response.status_code == 401


async def test_edit_page_shows_form_and_catalog_selects(client, store):
    order_id = await _escalated_order(store)
    _login(client)

    response = client.get(f"/review/orders/{order_id}/edit")

    assert response.status_code == 200
    assert "Resolve an unknown customer" in response.text
    assert "ChemFab Industries" in response.text
    assert "Sulfuric Acid" in response.text
    assert "GST override" in response.text
    assert "Save changes" in response.text


async def test_edit_post_updates_order_and_records_event(client, store):
    order_id = await _escalated_order(store)
    _login(client)

    response = _post(
        client,
        f"/review/orders/{order_id}/edit",
        data={
            "customer": "ChemFab Industries",
            "delivery_location": "Peenya Industrial Area",
            "items[0][product]": "sulfuric acid",
            "items[0][quantity]": "2000",
            "items[0][unit]": "kg",
            "gst_override_pct": "",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    order = store.orders[-1]
    assert order.customer == "ChemFab Industries"
    assert order.delivery_location == "Peenya Industrial Area"
    assert any(e.event_type == "order_edited" for e in store.events)


async def test_edit_post_resolves_unknown_customer_and_clears_badge(client, store):
    order_id = await _escalated_order(store)
    _login(client)
    assert "Unknown customer" in client.get("/review").text

    _post(
        client,
        f"/review/orders/{order_id}/edit",
        data={
            "customer": "",
            "delivery_location": "Peenya Industrial Area",
            "customer_id": "c_chemfab",
            "items[0][product]": "sulfuric acid",
            "items[0][quantity]": "2000",
            "items[0][unit]": "kg",
            "gst_override_pct": "",
        },
        follow_redirects=False,
    )

    order = store.orders[-1]
    assert order.customer_id == "c_chemfab"
    assert "Unknown customer" not in client.get("/review").text


async def test_edit_post_requires_login(client, store):
    order_id = await _escalated_order(store)
    response = client.post(
        f"/review/orders/{order_id}/edit",
        data={"customer": "ChemFab Industries"},
    )
    assert response.status_code == 401
    assert store.orders[-1].customer is None


async def test_gst_override_shown_on_detail_after_edit(client, store):
    order_id = await _escalated_order(store)
    _login(client)

    _post(
        client,
        f"/review/orders/{order_id}/edit",
        data={
            "customer": "",
            "delivery_location": "",
            "gst_override_pct": "18",
            "items[0][product]": "sulfuric acid",
            "items[0][quantity]": "2000",
            "items[0][unit]": "kg",
        },
        follow_redirects=False,
    )

    response = client.get(f"/review/orders/{order_id}")
    assert "GST override" in response.text
    assert "18%" in response.text


async def test_edit_rejected_order_reopens_and_appears_in_queue(client, store):
    order_id = await _escalated_order(store)
    _login(client)
    _post(client, f"/review/orders/{order_id}/reject")
    assert "No orders found" in client.get("/review").text

    _post(
        client,
        f"/review/orders/{order_id}/edit",
        data={
            "customer": "",
            "delivery_location": "Peenya Industrial Area",
            "gst_override_pct": "",
            "items[0][product]": "sulfuric acid",
            "items[0][quantity]": "2000",
            "items[0][unit]": "kg",
        },
        follow_redirects=False,
    )

    assert store.orders[-1].status is OrderStatus.PENDING_REVIEW
    assert "No orders found" not in client.get("/review").text


async def test_edit_post_bad_quantity_redirects_with_error(client, store):
    order_id = await _escalated_order(store)
    _login(client)

    response = _post(
        client,
        f"/review/orders/{order_id}/edit",
        data={
            "customer": "",
            "delivery_location": "",
            "gst_override_pct": "",
            "items[0][product]": "sulfuric acid",
            "items[0][quantity]": "ten",
            "items[0][unit]": "kg",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Could not save changes" in response.text


async def test_review_web_approve_of_a_late_order_heads_up_dispatch(store):
    # A human web approval landing after a 00:00 cutoff is late (issue #9):
    # the review web path must fire the same dispatch-channel heads-up as the
    # intake and approve tool paths.
    from src.seed_data import CONFIG
    from src.whatsapp import MockWhatsAppSender

    store.config = {**CONFIG, "cutoff_time": "00:00"}
    sender = MockWhatsAppSender()
    app = create_app(
        agent=build_agent(model=FakeEchoLlm(), store=store),
        session_service=InMemorySessionService(),
        store=store,
        whatsapp_sender=sender,
        web_passcode=PASSCODE,
        web_passcode_salt=PASSCODE_SALT,
        web_cookie_secure=False,
    )
    client = TestClient(app)
    await _escalated_order(store)
    _login(client)

    order_id = store.orders[-1].order_id
    response = _post(
        client, f"/review/orders/{order_id}/approve", follow_redirects=False
    )

    assert response.status_code == 303
    assert len(sender.sent) == 1
    assert sender.sent[0][1].startswith("Late order ")


def test_review_view_is_closed_when_passcode_unconfigured(store):
    app = create_app(
        agent=build_agent(model=FakeEchoLlm(), store=store),
        session_service=InMemorySessionService(),
        store=store,
    )
    response = TestClient(app).get("/review")
    assert response.status_code == 503


def test_review_login_is_closed_when_passcode_unconfigured(store):
    app = create_app(
        agent=build_agent(model=FakeEchoLlm(), store=store),
        session_service=InMemorySessionService(),
        store=store,
    )
    response = TestClient(app).post(
        "/review/login",
        data={"passcode": ""},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 503
