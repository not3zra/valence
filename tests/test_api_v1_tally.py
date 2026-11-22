"""REST + MCP Tally voucher push (issue #66)."""

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
from src.core import OrderProcessingCore
from src.mcp_auth import McpToolGateway, build_order_tool_operations
from src.order_actions import OrderActionService
from src.orders import Order, OrderItem
from src.store import InMemoryOrderStore
from src.tally import TallyPushService
from src.voucher import InMemoryVoucherStore
from src.web import create_app
from tests.fake_tally import (
    LINEERROR_XML,
    tally_url,
)

CUSTOMER_PHONE = "+919812345001"
APPROVER_PHONE = "+919845000001"


def _url(server):
    return tally_url(server)


async def _world():
    store, storage = InMemoryOrderStore(), InMemoryVoucherStore()

    async def approver_phones():
        return [approver.phone for approver in await store.get_approvers()]

    auth = AuthService(InMemoryAuthStore(), approver_phones=approver_phones)
    await auth.issue_admin_key(name="admin")
    await auth.issue_key(name="operator", role=Role.OPERATOR)
    await auth.issue_key(name="viewer", role=Role.VIEWER)
    approver = await auth.issue_key(
        name="approver", role=Role.APPROVER, phone=APPROVER_PHONE
    )

    class _Runtime:
        capabilities = None
        whatsapp_sender = None

        def __init__(self):
            from src.model_provider import ModelCapabilities

            self.capabilities = ModelCapabilities()
            self.store = store
            self.voucher_storage = storage

        def run_turn(self, **kwargs):
            return "fake reply"

        def stop(self):
            return None

    client = TestClient(create_app(runtime=_Runtime(), auth=auth))
    decision = await OrderProcessingCore(store).process(
        Order(
            order_id="ord_push_api",
            phone=CUSTOMER_PHONE,
            customer="ChemFab Industries",
            items=[OrderItem(product="sulfuric acid", quantity=2000, unit="kg")],
            delivery_location="Peenya Industrial Area",
            confidence=0.9,
        )
    )
    assert decision.approved, decision.escalation_reasons
    prepared = client.post(
        "/api/v1/orders/ord_push_api/actions",
        json={"action": "prepare_voucher", "confirm": True},
        headers={"Authorization": f"Bearer {approver.token}"},
    )
    assert prepared.status_code == 200, prepared.text
    return client, store, storage, auth, approver.token


def _run(coro):
    return asyncio.run(coro)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _events(store, order_id: str) -> list[str]:
    return [e.event_type for e in await store.list_order_events(order_id)]


def test_push_uses_instance_url_and_records_success_event(fake_tally):
    client, store, storage, auth, approver = _run(_world())
    _run(auth.set_integration("tally", {"url": _url(fake_tally)}))

    response = client.post(
        "/api/v1/orders/ord_push_api/actions",
        json={"action": "push_to_tally", "confirm": True},
        headers=_headers(approver),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action"] == "push_to_tally"
    assert fake_tally.received[0][2].decode() == storage.blobs["voucher_ord_push_api"]
    events = _run(_events(store, "ord_push_api"))
    assert events.count("voucher_pushed") == 1
    assert "voucher_push_failed" not in events


def test_line_error_is_surfaced_and_audited_as_failure(fake_tally):
    fake_tally.response_body = LINEERROR_XML.encode()
    client, store, storage, auth, approver = _run(_world())
    _run(auth.set_integration("tally", {"url": _url(fake_tally)}))

    response = client.post(
        "/api/v1/orders/ord_push_api/actions",
        json={"action": "push_to_tally", "confirm": True},
        headers=_headers(approver),
    )

    assert response.status_code == 502, response.text
    assert "Could not find Ledger" in response.json()["detail"]
    events = _run(_events(store, "ord_push_api"))
    assert events.count("voucher_push_failed") == 1


def test_push_requires_confirm_an_approver_and_a_configured_url():
    client, store, storage, auth, approver = _run(_world())
    url = "/api/v1/orders/ord_push_api/actions"

    unconfirmed = client.post(
        url, json={"action": "push_to_tally"}, headers=_headers(approver)
    )
    assert unconfirmed.status_code == 409
    for role in ("operator", "viewer"):
        token = _run(auth.issue_key(name=role, role=Role(role))).token
        assert (
            client.post(
                url,
                json={"action": "push_to_tally", "confirm": True},
                headers=_headers(token),
            ).status_code
            == 403
        )
    unconfigured = client.post(
        url,
        json={"action": "push_to_tally", "confirm": True},
        headers=_headers(approver),
    )
    assert unconfigured.status_code == 422
    assert "not configured" in unconfigured.json()["detail"].lower()
    assert "voucher_push_failed" in _run(_events(store, "ord_push_api"))


async def test_mcp_push_uses_the_same_service_and_rules(fake_tally):
    client, store, storage, auth, approver = await _world()
    await auth.set_integration("tally", {"url": _url(fake_tally)})

    async def tally_url() -> str:
        return await auth.integration_url("tally")

    service = OrderActionService(
        OrderProcessingCore(store),
        store,
        storage,
        TallyPushService(store, storage, tally_url),
    )
    gateway = McpToolGateway(auth, build_order_tool_operations(service))
    args = {"order_id": "ord_push_api", "action": "push_to_tally"}

    with pytest.raises(ConfirmationRequired):
        await gateway.call("order_action", api_key=approver, arguments=args)
    with pytest.raises(ForbiddenError):
        await gateway.call(
            "order_action",
            api_key=(await auth.issue_key(name="v", role=Role.VIEWER)).token,
            arguments={**args, "confirm": True},
        )
    result = await gateway.call(
        "order_action", api_key=approver, arguments={**args, "confirm": True}
    )
    assert result["action"] == "push_to_tally"
    assert fake_tally.received
    assert "voucher_pushed" in await _events(store, "ord_push_api")


def test_openapi_exposes_the_push_action():
    client, store, storage, auth, approver = _run(_world())
    schema = client.get("/openapi.json").json()
    actions = schema["components"]["schemas"]["OrderActionRequest"]["properties"][
        "action"
    ]["enum"]
    assert "push_to_tally" in actions
