"""REST v1 Loading Lists, stats and Cutoff render (issue #62)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from src.auth import AuthService, InMemoryAuthStore, Role
from src.dispatch import LateOrderNotifier
from src.loading_service import LoadingService
from src.model_provider import ModelCapabilities
from src.orders import Order, OrderItem, OrderStatus
from src.store import InMemoryOrderStore
from src.voucher import InMemoryVoucherStore
from src.web import create_app
from src.whatsapp import MockWhatsAppSender

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


class _Runtime:
    def __init__(self, store: InMemoryOrderStore, sender: MockWhatsAppSender) -> None:
        self.capabilities = ModelCapabilities()
        self.whatsapp_sender = sender
        self.store = store
        self.voucher_storage = InMemoryVoucherStore()

    def run_turn(self, **kwargs):
        return "fake reply"

    def stop(self) -> None:
        return None


def _world(
    now: datetime | None = None,
) -> tuple[TestClient, InMemoryOrderStore, MockWhatsAppSender, dict[str, str]]:
    store = InMemoryOrderStore()
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
    service = LoadingService(store, LateOrderNotifier(store, sender), now=now)
    client = TestClient(
        create_app(runtime=_Runtime(store, sender), auth=auth, loading_service=service)
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
        now=datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
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
