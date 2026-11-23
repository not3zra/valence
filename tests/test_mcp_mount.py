"""MCP mounted on the main Valence app (issue #96).

The hosted Agent Interface shares one port and one deployment with REST: the
streamable HTTP MCP endpoint lives at ``/mcp`` on the same app, and every tool
call is authorized by the caller's Bearer key just like a REST call.
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import quote

import httpx2
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from src.auth import AuthService, InMemoryAuthStore, Role
from src.config import settings
from src.core import OrderProcessingCore
from src.orders import Order, OrderItem
from src.store import InMemoryOrderStore
from src.web import create_app

from .fakes import FakeRuntime

PHONE = "+919812345001"
PROTOCOL_VERSION = "2025-06-18"
PROTOCOL_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


class _CommittingRuntime(FakeRuntime):
    """Runtime double whose turn commits a clean order through the real core."""

    def run_turn(self, *, sender_id, message, media=None):
        self.turns.append((sender_id, message, media))
        asyncio.run(
            OrderProcessingCore(self.store).process(
                Order(
                    phone=sender_id,
                    customer="ChemFab Industries",
                    items=[
                        OrderItem(
                            product="sulfuric acid", quantity=2000, unit="kg"
                        )
                    ],
                    delivery_location="Peenya Industrial Area",
                    confidence=0.9,
                    source_channel="whatsapp",
                )
            )
        )
        return "fake reply"


async def _build_app():
    store = InMemoryOrderStore()
    runtime = _CommittingRuntime(store=store)

    async def approver_phones():
        return [approver.phone for approver in await store.get_approvers()]

    auth = AuthService(InMemoryAuthStore(), approver_phones=approver_phones)
    await auth.create_instance(business_name="Acme Chemicals", timezone="UTC")
    admin = (await auth.issue_admin_key(name="admin")).token
    operator = (await auth.issue_key(name="operator", role=Role.OPERATOR)).token
    return create_app(runtime=runtime, auth=auth), store, admin, operator


def _app():
    return asyncio.run(_build_app())


def _session_headers(session: str, token: str | None = None) -> dict[str, str]:
    headers = {
        **PROTOCOL_HEADERS,
        "Mcp-Session-Id": session,
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _initialize(client: TestClient):
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mount-contract", "version": "1"},
            },
        },
        headers=PROTOCOL_HEADERS,
    )


def _tools_call(
    client: TestClient, session: str, name: str, arguments: dict, token=None
):
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers=_session_headers(session, token),
    )


def _sse_result(response):
    return json.loads(response.text.split("data: ", 1)[1].splitlines()[0])


def test_main_app_serves_mcp_and_rest_on_one_port():
    app, _, admin, _ = _app()

    with TestClient(app) as client:
        init = _initialize(client)
        assert init.status_code == 200, init.text
        session = init.headers["mcp-session-id"]
        result = _tools_call(client, session, "get_instance", {}, admin)
        health = client.get("/health")
        instance = client.get(
            f"/api/v1/instance?business_name={quote('Acme')}",
            headers={"Authorization": f"Bearer {admin}"},
        )

    assert _sse_result(result)["result"]["isError"] is False
    assert "Acme Chemicals" in _sse_result(result)["result"]["content"][0]["text"]
    assert health.json() == {"status": "ok"}
    assert instance.status_code == 200


def test_mounted_mcp_rejects_calls_without_the_callers_bearer_key(monkeypatch):
    app, _, admin, _ = _app()
    monkeypatch.setattr(settings, "valence_api_key", admin)

    with TestClient(app) as client:
        init = _initialize(client)
        assert init.status_code == 200, init.text
        session = init.headers["mcp-session-id"]
        denied = _tools_call(client, session, "get_instance", {})
        allowed = _tools_call(client, session, "get_instance", {}, admin)

    assert _sse_result(denied)["result"]["isError"] is True
    assert "invalid or revoked API key" in _sse_result(denied)["result"]["content"][0][
        "text"
    ]
    assert _sse_result(allowed)["result"]["isError"] is False


def test_mounted_mcp_enforces_the_rest_role_rules():
    app, _, _, operator = _app()

    with TestClient(app) as client:
        init = _initialize(client)
        assert init.status_code == 200, init.text
        session = init.headers["mcp-session-id"]
        denied = _tools_call(
            client,
            session,
            "order_action",
            {"order_id": "ord_1", "action": "approve", "confirm": True},
            operator,
        )

    assert _sse_result(denied)["result"]["isError"] is True
    assert "role operator" in _sse_result(denied)["result"]["content"][0]["text"]


async def test_real_mcp_client_completes_an_intake_through_the_mounted_app():
    app, store, _, operator = await _build_app()

    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {operator}"},
        ) as client:
            async with streamable_http_client(
                "http://testserver/mcp", http_client=client
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        "intake", {"sender_id": PHONE, "message": "2000 kg acid"}
                    )

    assert result.is_error is False
    intake = json.loads(result.content[0].text)
    assert intake["decision"]["status"] == "approved"
    assert any(order.phone == PHONE for order in await store.list_all_orders())
