"""REST + MCP order editing and approved actions (issue #60)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from src.auth import (
    AuthService,
    ConfirmationRequired,
    ForbiddenError,
    InMemoryAuthStore,
    Role,
)
from src.core import ApprovalError, OrderProcessingCore
from src.mcp_auth import McpToolGateway, build_order_tool_operations
from src.order_actions import OrderActionService
from src.orders import Order, OrderItem, OrderStatus
from src.store import InMemoryOrderStore
from src.web import create_app

CUSTOMER_PHONE = "+919812345001"
APPROVER_PHONE = "+919845000001"


async def _new_world():
    store = InMemoryOrderStore()
    runtime = _Runtime(store)

    async def approver_phones():
        return [approver.phone for approver in await store.get_approvers()]

    auth = AuthService(InMemoryAuthStore(), approver_phones=approver_phones)
    admin = (await auth.issue_admin_key(name="admin")).token
    operator = (await auth.issue_key(name="operator", role=Role.OPERATOR)).token
    viewer = (await auth.issue_key(name="viewer", role=Role.VIEWER)).token
    approver_key = await auth.issue_key(
        name="approver", role=Role.APPROVER, phone=APPROVER_PHONE
    )
    keys = {
        "admin": admin,
        "operator": operator,
        "viewer": viewer,
        "approver": approver_key.token,
        "approver_id": approver_key.key_id,
    }
    return store, runtime, auth, keys


def _client():
    store, runtime, auth, keys = asyncio.run(_new_world())
    return TestClient(create_app(runtime=runtime, auth=auth)), store, auth, keys


async def _aclient():
    store, runtime, auth, keys = await _new_world()
    return TestClient(create_app(runtime=runtime, auth=auth)), store, auth, keys


class _Runtime:
    def __init__(self, store) -> None:
        self.store = store
        self.voucher_storage = None
        self.whatsapp_sender = None

    @property
    def capabilities(self):
        from src.model_provider import ModelCapabilities

        return ModelCapabilities()

    def run_turn(self, **kwargs) -> str:
        return "fake reply"

    def stop(self) -> None:
        return None


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _an_order(order_id: str, status: OrderStatus) -> Order:
    return Order(
        order_id=order_id,
        phone=CUSTOMER_PHONE,
        customer=None,
        items=[OrderItem(product="sulfuric acid", quantity=2000, unit="kg")],
        delivery_location=None,
        confidence=0.9,
        status=status,
    )


def _seed_order(store, order_id: str, status: OrderStatus = OrderStatus.PENDING_REVIEW):
    order = _an_order(order_id, status)
    asyncio.run(store.create_order(order))
    return order


async def _aseed_order(
    store, order_id: str, status: OrderStatus = OrderStatus.PENDING_REVIEW
):
    order = _an_order(order_id, status)
    await store.create_order(order)
    return order


def _event_types(events) -> list[str]:
    return [event.event_type for event in events]


def _events(store, order_id: str) -> list[str]:
    return _event_types(asyncio.run(store.list_order_events(order_id)))


async def _aevents(store, order_id: str) -> list[str]:
    return _event_types(await store.list_order_events(order_id))


def test_approver_can_edit_order_fields_and_every_edit_writes_an_event():
    client, store, auth, keys = _client()
    _seed_order(store, "ord_edit")

    response = client.patch(
        "/api/v1/orders/ord_edit",
        json={
            "customer": "ChemFab Industries",
            "delivery_location": "Peenya Industrial Area",
        },
        headers=_headers(keys["approver"]),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["customer"] == "ChemFab Industries"
    assert body["delivery_location"] == "Peenya Industrial Area"
    assert "order_edited" in _events(store, "ord_edit")
    edited = [
        event
        for event in asyncio.run(store.list_order_events("ord_edit"))
        if event.event_type == "order_edited"
    ]
    assert edited[-1].payload["by"] == keys["approver_id"]


def test_edit_requires_an_approver_principal():
    client, store, auth, keys = _client()
    _seed_order(store, "ord_edit")

    body = {"customer": "ChemFab Industries"}
    assert client.patch("/api/v1/orders/ord_edit", json=body).status_code == 401
    assert (
        client.patch(
            "/api/v1/orders/ord_edit", json=body, headers=_headers(keys["viewer"])
        ).status_code
        == 403
    )
    assert (
        client.patch(
            "/api/v1/orders/ord_edit", json=body, headers=_headers(keys["operator"])
        ).status_code
        == 403
    )
    assert _events(store, "ord_edit") == []


def test_approver_confirms_approve_and_the_decision_is_audited():
    client, store, auth, keys = _client()
    _seed_order(store, "ord_act")

    response = client.post(
        "/api/v1/orders/ord_act/actions",
        json={"action": "approve", "confirm": True},
        headers=_headers(keys["approver"]),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action"] == "approve"
    assert body["status"] == "approved"
    assert body["order"]["status"] == "approved"
    events = _events(store, "ord_act")
    assert "order_approved" in events
    approved = [
        event
        for event in asyncio.run(store.list_order_events("ord_act"))
        if event.event_type == "order_approved"
    ]
    assert approved[-1].payload["approved_by"] == APPROVER_PHONE


def test_approver_can_reject_and_dispatch_through_the_same_route():
    client, store, auth, keys = _client()
    _seed_order(store, "ord_reject")
    _seed_order(store, "ord_dispatch", OrderStatus.APPROVED)

    rejected = client.post(
        "/api/v1/orders/ord_reject/actions",
        json={"action": "reject", "confirm": True},
        headers=_headers(keys["approver"]),
    )
    dispatched = client.post(
        "/api/v1/orders/ord_dispatch/actions",
        json={"action": "dispatch", "confirm": True},
        headers=_headers(keys["approver"]),
    )

    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"
    assert dispatched.status_code == 200, dispatched.text
    assert dispatched.json()["status"] == "dispatched"
    assert "order_rejected" in _events(store, "ord_reject")
    assert "order_dispatched" in _events(store, "ord_dispatch")


def test_privileged_actions_require_an_explicit_confirmation():
    client, store, auth, keys = _client()
    _seed_order(store, "ord_act")

    response = client.post(
        "/api/v1/orders/ord_act/actions",
        json={"action": "approve"},
        headers=_headers(keys["approver"]),
    )

    assert response.status_code == 409
    assert _events(store, "ord_act") == []
    assert asyncio.run(store.get_order("ord_act")).status is OrderStatus.PENDING_REVIEW


def test_actions_require_an_approver_principal():
    client, store, auth, keys = _client()
    _seed_order(store, "ord_act")
    body = {"action": "approve", "confirm": True}

    assert client.post("/api/v1/orders/ord_act/actions", json=body).status_code == 401
    assert (
        client.post(
            "/api/v1/orders/ord_act/actions",
            json=body,
            headers=_headers(keys["viewer"]),
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/orders/ord_act/actions",
            json=body,
            headers=_headers(keys["operator"]),
        ).status_code
        == 403
    )
    assert _events(store, "ord_act") == []


def test_unknown_and_illegal_actions_are_rejected():
    client, store, auth, keys = _client()
    _seed_order(store, "ord_act")
    _seed_order(store, "ord_approved", OrderStatus.APPROVED)

    unknown = client.post(
        "/api/v1/orders/ord_act/actions",
        json={"action": "drop_table", "confirm": True},
        headers=_headers(keys["approver"]),
    )
    illegal = client.post(
        "/api/v1/orders/ord_approved/actions",
        json={"action": "approve", "confirm": True},
        headers=_headers(keys["approver"]),
    )
    early_dispatch = client.post(
        "/api/v1/orders/ord_act/actions",
        json={"action": "dispatch", "confirm": True},
        headers=_headers(keys["approver"]),
    )

    assert unknown.status_code == 422
    assert illegal.status_code == 422
    assert early_dispatch.status_code == 422
    assert _events(store, "ord_act") == []
    assert _events(store, "ord_approved") == []


def test_openapi_exposes_the_edit_and_actions_contract():
    client, store, auth, keys = _client()

    paths = client.get("/openapi.json").json()["paths"]

    assert "/api/v1/orders/{order_id}" in paths
    assert "patch" in paths["/api/v1/orders/{order_id}"]
    assert "/api/v1/orders/{order_id}/actions" in paths
    assert "post" in paths["/api/v1/orders/{order_id}/actions"]


def _gateway(auth, store):
    service = OrderActionService(OrderProcessingCore(store), store)
    return McpToolGateway(auth, build_order_tool_operations(service))


async def test_mcp_edit_enforces_the_approver_rule_and_the_same_audit_trail():
    client, store, auth, keys = await _aclient()
    await _aseed_order(store, "ord_mcp_edit")
    gateway = _gateway(auth, store)

    with pytest.raises(ForbiddenError):
        await gateway.call(
            "edit_order",
            api_key=keys["viewer"],
            arguments={"order_id": "ord_mcp_edit", "changes": {"customer": "Nope"}},
        )

    result = await gateway.call(
        "edit_order",
        api_key=keys["approver"],
        arguments={
            "order_id": "ord_mcp_edit",
            "changes": {"customer": "ChemFab Industries"},
        },
    )
    assert result["customer"] == "ChemFab Industries"
    assert (await store.get_order("ord_mcp_edit")).customer == "ChemFab Industries"
    assert "order_edited" in await _aevents(store, "ord_mcp_edit")


async def test_mcp_order_action_requires_confirm_and_rejects_non_approvers():
    client, store, auth, keys = await _aclient()
    await _aseed_order(store, "ord_mcp_act")
    gateway = _gateway(auth, store)

    with pytest.raises(ConfirmationRequired):
        await gateway.call(
            "order_action",
            api_key=keys["approver"],
            arguments={"order_id": "ord_mcp_act", "action": "approve"},
        )
    with pytest.raises(ForbiddenError):
        await gateway.call(
            "order_action",
            api_key=keys["operator"],
            arguments={"order_id": "ord_mcp_act", "action": "approve", "confirm": True},
        )
    assert await _aevents(store, "ord_mcp_act") == []

    result = await gateway.call(
        "order_action",
        api_key=keys["approver"],
        arguments={"order_id": "ord_mcp_act", "action": "approve", "confirm": True},
    )
    assert result["status"] == "approved"
    assert (await _aevents(store, "ord_mcp_act")).count("order_approved") == 1


async def test_mcp_rejects_illegal_transitions_and_unimplemented_actions():
    client, store, auth, keys = await _aclient()
    await _aseed_order(store, "ord_mcp_approved", OrderStatus.APPROVED)
    await _aseed_order(store, "ord_mcp_pending")
    gateway = _gateway(auth, store)

    with pytest.raises(ApprovalError):
        await gateway.call(
            "order_action",
            api_key=keys["approver"],
            arguments={
                "order_id": "ord_mcp_approved",
                "action": "approve",
                "confirm": True,
            },
        )
    with pytest.raises(ValueError):
        await gateway.call(
            "order_action",
            api_key=keys["approver"],
            arguments={
                "order_id": "ord_mcp_pending",
                "action": "bill",
                "confirm": True,
            },
        )
    assert await _aevents(store, "ord_mcp_approved") == []
    assert await _aevents(store, "ord_mcp_pending") == []
