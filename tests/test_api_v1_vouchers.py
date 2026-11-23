"""REST voucher preparation, download and billing status (issue #61)."""

from __future__ import annotations

import asyncio
import urllib.request
from dataclasses import replace
from xml.etree import ElementTree as ET

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
from src.model_provider import ModelCapabilities
from src.order_actions import OrderActionService
from src.orders import Order, OrderItem
from src.seed_data import PRODUCTS
from src.store import InMemoryOrderStore
from src.voucher import InMemoryVoucherStore
from src.web import create_app

CUSTOMER_PHONE = "+919812345001"
APPROVER_PHONE = "+919845000001"


class _Runtime:
    def __init__(self, store, storage) -> None:
        self.store = store
        self.voucher_storage = storage
        self.whatsapp_sender = None

    @property
    def capabilities(self):
        return ModelCapabilities()

    def run_turn(self, **kwargs) -> str:
        return "fake reply"

    async def run_turn_async(self, **kwargs) -> str:
        return await asyncio.to_thread(self.run_turn, **kwargs)

    def stop(self) -> None:
        return None


async def _world(products=None):
    store = InMemoryOrderStore(products=products) if products else InMemoryOrderStore()
    storage = InMemoryVoucherStore()
    runtime = _Runtime(store, storage)

    async def approver_phones():
        return [approver.phone for approver in await store.get_approvers()]

    auth = AuthService(InMemoryAuthStore(), approver_phones=approver_phones)
    await auth.issue_admin_key(name="admin")
    operator = (await auth.issue_key(name="operator", role=Role.OPERATOR)).token
    viewer = (await auth.issue_key(name="viewer", role=Role.VIEWER)).token
    approver = await auth.issue_key(
        name="approver", role=Role.APPROVER, phone=APPROVER_PHONE
    )
    keys = {
        "operator": operator,
        "viewer": viewer,
        "approver": approver.token,
        "approver_id": approver.key_id,
    }
    return store, storage, runtime, auth, keys


def _client(products=None):
    store, storage, runtime, auth, keys = asyncio.run(_world(products))
    return TestClient(create_app(runtime=runtime, auth=auth)), store, storage, keys


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _events(store, order_id: str) -> list[str]:
    return [
        event.event_type
        for event in asyncio.run(store.list_order_events(order_id))
    ]


async def _seed_approved(store, order_id: str = "ord_voucher") -> str:
    decision = await OrderProcessingCore(store).process(
        Order(
            order_id=order_id,
            phone=CUSTOMER_PHONE,
            customer="ChemFab Industries",
            items=[OrderItem(product="sulfuric acid", quantity=2000, unit="kg")],
            delivery_location="Peenya Industrial Area",
            confidence=0.9,
        )
    )
    assert decision.approved, decision.escalation_reasons
    return decision.order_id


def test_confirmed_prepare_voucher_locks_amounts_and_writes_the_event():
    client, store, storage, keys = _client()
    order_id = asyncio.run(_seed_approved(store))

    response = client.post(
        f"/api/v1/orders/{order_id}/actions",
        json={"action": "prepare_voucher", "confirm": True},
        headers=_headers(keys["approver"]),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action"] == "prepare_voucher"
    assert body["order"]["voucher"] == {
        "prepared": True,
        "voucher_id": "voucher_ord_voucher",
    }
    assert "voucher_ready" in _events(store, order_id)
    xml = storage.blobs["voucher_ord_voucher"]
    root = ET.fromstring(xml)
    assert root.findtext(".//VOUCHERNUMBER") == "voucher_ord_voucher"
    assert root.findtext(".//RATE") == "17.50"
    ledgers = {
        entry.findtext("LEDGERNAME"): entry.findtext("AMOUNT")
        for entry in root.findall(".//LEDGERENTRIES.LIST")
    }
    assert ledgers["CHEMFAB INDUSTRIES"] == "-41300.00"
    assert ledgers["Output CGST 9%"] == "3150.00"
    assert ledgers["Output SGST 9%"] == "3150.00"


def test_approver_downloads_the_prepared_voucher_xml():
    client, store, storage, keys = _client()
    order_id = asyncio.run(_seed_approved(store))
    assert (
        client.post(
            f"/api/v1/orders/{order_id}/actions",
            json={"action": "prepare_voucher", "confirm": True},
            headers=_headers(keys["approver"]),
        ).status_code
        == 200
    )

    response = client.get(
        f"/api/v1/orders/{order_id}/voucher", headers=_headers(keys["approver"])
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/xml")
    assert ET.fromstring(response.text).findtext(".//RATE") == "17.50"


def test_voucher_download_is_approver_only_and_needs_a_prepared_voucher():
    client, store, storage, keys = _client()
    order_id = asyncio.run(_seed_approved(store))
    url = f"/api/v1/orders/{order_id}/voucher"

    assert client.get(url).status_code == 401
    assert client.get(url, headers=_headers(keys["viewer"])).status_code == 403
    assert client.get(url, headers=_headers(keys["operator"])).status_code == 403
    unprepared = client.get(url, headers=_headers(keys["approver"]))
    assert unprepared.status_code == 422
    assert "no prepared voucher" in unprepared.json()["detail"]
    assert client.get(
        "/api/v1/orders/ord_missing/voucher", headers=_headers(keys["approver"])
    ).status_code == 404


def test_confirmed_bill_marks_the_order_billed_and_audits_the_approver():
    client, store, storage, keys = _client()
    order_id = asyncio.run(_seed_approved(store))
    assert (
        client.post(
            f"/api/v1/orders/{order_id}/actions",
            json={"action": "prepare_voucher", "confirm": True},
            headers=_headers(keys["approver"]),
        ).status_code
        == 200
    )

    response = client.post(
        f"/api/v1/orders/{order_id}/actions",
        json={"action": "bill", "confirm": True},
        headers=_headers(keys["approver"]),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action"] == "bill"
    assert body["status"] == "billed"
    assert body["order"]["status"] == "billed"
    assert body["order"]["voucher"]["prepared"] is True
    billed = [
        event
        for event in asyncio.run(store.list_order_events(order_id))
        if event.event_type == "order_billed"
    ]
    assert len(billed) == 1
    assert billed[0].payload["voucher_id"] == "voucher_ord_voucher"
    assert billed[0].payload["by"] == keys["approver_id"]


def test_bill_requires_confirm_an_approver_and_a_prepared_voucher():
    client, store, storage, keys = _client()
    order_id = asyncio.run(_seed_approved(store))
    url = f"/api/v1/orders/{order_id}/actions"

    unconfirmed = client.post(
        url, json={"action": "bill"}, headers=_headers(keys["approver"])
    )
    assert unconfirmed.status_code == 409
    assert (
        client.post(
            url,
            json={"action": "bill", "confirm": True},
            headers=_headers(keys["viewer"]),
        ).status_code
        == 403
    )
    unprepared = client.post(
        url,
        json={"action": "bill", "confirm": True},
        headers=_headers(keys["approver"]),
    )
    assert unprepared.status_code == 422
    assert "no prepared voucher" in unprepared.json()["detail"]
    assert "order_billed" not in _events(store, order_id)


def test_prepare_voucher_requires_confirm_and_an_approver():
    client, store, storage, keys = _client()
    order_id = asyncio.run(_seed_approved(store))
    url = f"/api/v1/orders/{order_id}/actions"

    assert (
        client.post(
            url, json={"action": "prepare_voucher"}, headers=_headers(keys["approver"])
        ).status_code
        == 409
    )
    assert (
        client.post(
            url,
            json={"action": "prepare_voucher", "confirm": True},
            headers=_headers(keys["operator"]),
        ).status_code
        == 403
    )
    assert storage.blobs == {}
    assert "voucher_ready" not in _events(store, order_id)


def test_unmapped_master_blocks_preparation_with_an_explicit_error():
    unmapped = [
        replace(product, stock_item="UNMAPPED")
        if product.id == "p_sulfuric98"
        else product
        for product in PRODUCTS
    ]
    client, store, storage, keys = _client(unmapped)
    order_id = asyncio.run(_seed_approved(store))

    response = client.post(
        f"/api/v1/orders/{order_id}/actions",
        json={"action": "prepare_voucher", "confirm": True},
        headers=_headers(keys["approver"]),
    )

    assert response.status_code == 422
    assert "stock item" in response.json()["detail"]
    assert storage.blobs == {}
    assert "voucher_ready" not in _events(store, order_id)


async def test_mcp_voucher_actions_and_download_use_the_same_service_and_rules():
    store, storage, runtime, auth, keys = await _world()
    order_id = await _seed_approved(store, "ord_mcp_voucher")
    service = OrderActionService(OrderProcessingCore(store), store, storage)
    gateway = McpToolGateway(auth, build_order_tool_operations(service))
    arguments = {"order_id": order_id, "action": "prepare_voucher"}

    with pytest.raises(ConfirmationRequired):
        await gateway.call(
            "order_action", api_key=keys["approver"], arguments=arguments
        )
    with pytest.raises(ForbiddenError):
        await gateway.call(
            "order_action",
            api_key=keys["viewer"],
            arguments={**arguments, "confirm": True},
        )
    assert storage.blobs == {}

    prepared = await gateway.call(
        "order_action",
        api_key=keys["approver"],
        arguments={**arguments, "confirm": True},
    )
    assert prepared["order"]["voucher"]["prepared"] is True
    xml = await gateway.call(
        "download_voucher",
        api_key=keys["approver"],
        arguments={"order_id": order_id},
    )
    assert "<VOUCHERNUMBER>voucher_ord_mcp_voucher</VOUCHERNUMBER>" in xml
    with pytest.raises(ForbiddenError):
        await gateway.call(
            "download_voucher",
            api_key=keys["viewer"],
            arguments={"order_id": order_id},
        )

    billed = await gateway.call(
        "order_action",
        api_key=keys["approver"],
        arguments={"order_id": order_id, "action": "bill", "confirm": True},
    )
    assert billed["status"] == "billed"
    events = await store.list_order_events(order_id)
    assert "voucher_ready" in [event.event_type for event in events]
    billed_by = [
        event.payload["by"] for event in events if event.event_type == "order_billed"
    ]
    assert billed_by == [keys["approver_id"]]


def test_openapi_exposes_the_voucher_download_contract():
    client, store, storage, keys = _client()

    paths = client.get("/openapi.json").json()["paths"]

    assert "/api/v1/orders/{order_id}/voucher" in paths
    assert "get" in paths["/api/v1/orders/{order_id}/voucher"]


def test_voucher_flow_makes_no_tally_network_call(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("voucher ticket made an outbound Tally call")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    client, store, storage, keys = _client()
    order_id = asyncio.run(_seed_approved(store))
    headers = _headers(keys["approver"])

    for action in ("prepare_voucher", "bill"):
        response = client.post(
            f"/api/v1/orders/{order_id}/actions",
            json={"action": action, "confirm": True},
            headers=headers,
        )
        assert response.status_code == 200, response.text
    downloaded = client.get(f"/api/v1/orders/{order_id}/voucher", headers=headers)
    assert downloaded.status_code == 200, downloaded.text
