"""REST v1 Loading Lists, stats and Cutoff render (issue #62)."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

from fastapi.testclient import TestClient

from src.api_v1 import MAX_LOADING_LIST_BODY_BYTES
from src.auth import AuthService, InMemoryAuthStore, Role
from src.dispatch import LateOrderNotifier
from src.loading_service import LoadingService
from src.orders import Order, OrderItem, OrderStatus
from src.store import InMemoryOrderStore
from src.voucher import InMemoryVoucherStore
from src.web import create_app
from src.whatsapp import MockWhatsAppSender

from .fakes import FakeRuntime

DAY = "2026-08-14"
EARLY = "2026-08-14T10:00:00+00:00"
LATE = "2026-08-14T13:00:00+00:00"


def _order(
    order_id: str,
    *,
    location_id: str = "dl_peenya",
    when: str = EARLY,
    status: OrderStatus = OrderStatus.APPROVED,
) -> Order:
    return Order(
        order_id=order_id,
        phone="+919812345001",
        customer="ChemFab Industries",
        delivery_location="Peenya Industrial Area",
        delivery_location_id=location_id,
        items=[OrderItem(product="Sulfuric Acid", quantity=2000, unit="kg")],
        status=status,
        created_at=when,
        updated_at=when,
    )


def _world(
    now: datetime | None = None,
    *,
    seed: bool = True,
) -> tuple[TestClient, InMemoryOrderStore, MockWhatsAppSender, dict[str, str]]:
    store = InMemoryOrderStore()
    if seed:
        store.orders.extend(
            [
                _order("ord_west", location_id="dl_peenya"),
                _order("ord_east", location_id="dl_whitefield"),
                _order("ord_late", when=LATE),
                _order("ord_pending", status=OrderStatus.PENDING_REVIEW),
            ]
        )
    sender = MockWhatsAppSender()

    async def approver_phones():
        return [approver.phone for approver in await store.get_approvers()]

    auth = AuthService(InMemoryAuthStore(), approver_phones=approver_phones)
    tokens = {
        "admin": asyncio.run(auth.issue_admin_key(name="admin")).token,
        "operator": asyncio.run(
            auth.issue_key(name="operator", role=Role.OPERATOR)
        ).token,
        "viewer": asyncio.run(auth.issue_key(name="viewer", role=Role.VIEWER)).token,
        "approver": asyncio.run(
            auth.issue_key(
                name="approver", role=Role.APPROVER, phone="+919845000001"
            )
        ).token,
    }
    runtime = FakeRuntime(
        store=store,
        voucher_storage=InMemoryVoucherStore(),
        whatsapp_sender=sender,
    )
    service = LoadingService(store, LateOrderNotifier(store, sender), now=now)
    client = TestClient(
        create_app(runtime=runtime, auth=auth, loading_service=service)
    )
    return client, store, sender, tokens


def _headers(role: str, tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens[role]}"}


def test_loading_lists_requires_authentication():
    client, _, _, _ = _world()
    assert client.get("/api/v1/loading-lists").status_code == 401


def test_loading_lists_groups_routes_and_late_addons_for_a_delivery_day():
    client, _, _, tokens = _world()

    response = client.get(
        "/api/v1/loading-lists",
        params={"day": DAY},
        headers=_headers("viewer", tokens),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["delivery_day"] == DAY
    assert {section["route_name"] for section in body["sections"]} == {
        "Bengaluru West",
        "Bengaluru East",
    }
    assert [entry["order_id"] for entry in body["late"]] == ["ord_late"]
    assert "ord_pending" not in str(body)
    rendered = str(body).lower()
    assert "rate" not in rendered and "inr" not in rendered


def test_loading_lists_rejects_a_malformed_delivery_day():
    client, _, _, tokens = _world()
    response = client.get(
        "/api/v1/loading-lists",
        params={"day": "not-a-date"},
        headers=_headers("admin", tokens),
    )
    assert response.status_code == 422


def test_stats_requires_authentication():
    client, _, _, _ = _world()
    assert client.get("/api/v1/stats").status_code == 401


def test_stats_reports_pending_approved_billed_and_live_late_counts():
    client, _, _, tokens = _world(
        now=datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    )

    response = client.get("/api/v1/stats", headers=_headers("viewer", tokens))

    assert response.status_code == 200, response.text
    assert response.json() == {
        "pending_escalations": 1,
        "approved_today": 3,
        "billed_today": 0,
        "late_orders": 1,
    }


def test_cutoff_render_requires_an_admin_principal():
    client, _, _, tokens = _world()

    assert client.post("/api/v1/cutoff/render").status_code == 401
    for role in ("operator", "viewer", "approver"):
        response = client.post(
            "/api/v1/cutoff/render", headers=_headers(role, tokens)
        )
        assert response.status_code == 403, (role, response.text)


def test_cutoff_render_notifies_late_orders_and_returns_the_live_list():
    client, store, sender, tokens = _world()

    response = client.post(
        "/api/v1/cutoff/render",
        params={"day": DAY},
        headers=_headers("admin", tokens),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["delivery_day"] == DAY
    assert [entry["order_id"] for entry in body["late"]] == ["ord_late"]
    assert len(sender.sent) == 1
    assert sender.sent[0][0] == "+919845000003"
    events = asyncio.run(store.list_order_events("ord_late"))
    assert [event.event_type for event in events] == ["order_late"]


def test_cutoff_render_rejects_a_malformed_delivery_day():
    client, _, _, tokens = _world()
    response = client.post(
        "/api/v1/cutoff/render",
        params={"day": "not-a-date"},
        headers=_headers("admin", tokens),
    )
    assert response.status_code == 422


def test_loading_lists_post_requires_an_admin_principal():
    client, _, _, tokens = _world()

    assert client.post("/api/v1/loading-lists", json={}).status_code == 401
    for role in ("operator", "viewer", "approver"):
        response = client.post(
            "/api/v1/loading-lists", json={}, headers=_headers(role, tokens)
        )
        assert response.status_code == 403, (role, response.text)


def test_loading_lists_post_matches_cutoff_render_for_the_same_day():
    client, store, sender, tokens = _world()
    headers = _headers("admin", tokens)

    created = client.post("/api/v1/loading-lists", json={"day": DAY}, headers=headers)
    legacy = client.post("/api/v1/cutoff/render", params={"day": DAY}, headers=headers)

    assert created.status_code == 200, created.text
    assert created.json() == legacy.json()
    assert created.json()["delivery_day"] == DAY
    assert [entry["order_id"] for entry in created.json()["late"]] == ["ord_late"]
    assert len(sender.sent) == 1
    events = asyncio.run(store.list_order_events("ord_late"))
    assert [event.event_type for event in events] == ["order_late"]


def test_loading_lists_post_rejects_a_malformed_delivery_day():
    client, _, _, tokens = _world()
    response = client.post(
        "/api/v1/loading-lists",
        json={"day": "not-a-date"},
        headers=_headers("admin", tokens),
    )
    assert response.status_code == 422


def test_loading_lists_post_renders_an_empty_list_for_an_idle_instance():
    client, _, _, tokens = _world(seed=False)
    response = client.post(
        "/api/v1/loading-lists",
        json={"day": DAY},
        headers=_headers("admin", tokens),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["delivery_day"] == DAY
    assert body["sections"] == []
    assert body["unrouted"] == []
    assert body["late"] == []


def test_loading_lists_post_rejects_an_oversized_body():
    client, _, _, tokens = _world()
    body = b'{"day": "' + b"x" * (MAX_LOADING_LIST_BODY_BYTES + 1) + b'"}'
    response = client.post(
        "/api/v1/loading-lists",
        content=body,
        headers={"content-type": "application/json", **_headers("admin", tokens)},
    )
    assert response.status_code == 413


def test_loading_lists_post_checks_auth_before_reading_the_body():
    client, _, _, _ = _world()
    response = client.post(
        "/api/v1/loading-lists",
        content=b"x" * (MAX_LOADING_LIST_BODY_BYTES + 1),
        headers={"content-type": "application/json", "Authorization": "Bearer nope"},
    )
    assert response.status_code == 401


def test_loading_lists_post_without_a_day_renders_the_current_window():
    client, _, _, tokens = _world()
    response = client.post(
        "/api/v1/loading-lists", json={}, headers=_headers("admin", tokens)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert date.fromisoformat(body["delivery_day"])
    assert {section["route_name"] for section in body["sections"]} == {
        "Bengaluru West",
        "Bengaluru East",
    }


def test_openapi_documents_the_loading_lists_endpoint_and_its_alias():
    client, _, _, _ = _world()

    schema = client.get("/openapi.json").json()
    paths = schema["paths"]

    created = paths["/api/v1/loading-lists"]["post"]
    assert "loading" in created["tags"]
    request_schema = created["requestBody"]["content"]["application/json"]["schema"]
    assert "day" in request_schema["properties"]

    alias = paths["/api/v1/cutoff/render"]["post"]
    assert alias["deprecated"] is True
    assert "compatible alias" in alias["description"].lower()


def test_loading_lists_post_renders_a_date_window():
    client, _, _, tokens = _world()
    response = client.post(
        "/api/v1/loading-lists",
        json={"window": {"start": DAY, "end": "2026-08-16"}},
        headers=_headers("admin", tokens),
    )
    assert response.status_code == 200, response.text
    assert [item["delivery_day"] for item in response.json()] == [
        "2026-08-14",
        "2026-08-15",
        "2026-08-16",
    ]


def test_loading_lists_post_rejects_an_invalid_window():
    client, _, _, tokens = _world()
    for window in (
        {"start": "2026-08-16", "end": "2026-08-14"},
        {"start": "not-a-date", "end": DAY},
        {"start": "2026-08-14", "end": "2026-12-31"},
    ):
        response = client.post(
            "/api/v1/loading-lists",
            json={"window": window},
            headers=_headers("admin", tokens),
        )
        assert response.status_code == 422, (window, response.text)


def test_loading_lists_post_rejects_a_day_and_window_together():
    client, _, _, tokens = _world()
    response = client.post(
        "/api/v1/loading-lists",
        json={"day": DAY, "window": {"start": DAY, "end": DAY}},
        headers=_headers("admin", tokens),
    )
    assert response.status_code == 422
