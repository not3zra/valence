"""MCP server over the authorized service layer (issue #63).

The server is one transport adapter around the same services REST uses: every
tool call goes through ``McpToolGateway``, so a connected agent cannot widen its
role, skip a confirmation, or invent an idempotent retry.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace

import pytest
from mcp.client.session import ClientSession
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.memory import create_client_server_memory_streams
from starlette.applications import Starlette
from starlette.testclient import TestClient

from src.auth import AuthService, InMemoryAuthStore, Role
from src.intake import IntakeResult, IntakeService
from src.mcp_auth import DEFAULT_POLICIES, McpToolGateway
from src.mcp_server import (
    MCP_TOOL_NAMES,
    build_mcp_operations,
    build_mcp_server,
    run_mcp_server,
    serve_mcp_streams,
    streamable_http_app,
)
from src.model_provider import ModelCapabilities
from src.store import InMemoryOrderStore

PHONE = "+919812345001"


async def _async(value):
    return value


async def _auth():
    service = AuthService(
        InMemoryAuthStore(), approver_phones=lambda: _async([PHONE])
    )
    await service.create_instance(business_name="Acme Chemicals", timezone="UTC")
    admin = await service.issue_admin_key(name="admin")
    operator = await service.issue_key(name="operator", role=Role.OPERATOR)
    approver = await service.issue_key(
        name="approver", role=Role.APPROVER, phone=PHONE
    )
    viewer = await service.issue_key(name="viewer", role=Role.VIEWER)
    return service, admin.token, operator.token, approver.token, viewer.token


class _Admin:
    async def list_catalog(self, kind):
        return [{"id": "p1", "kind": kind}]

    async def get_config(self):
        return {"cutoff_time": "16:00"}


class _Intake:
    def __init__(self):
        self.calls: list[tuple] = []

    async def intake_text(
        self,
        *,
        sender_id,
        message,
        principal_id,
        idempotency_key=None,
        fingerprint="",
    ):
        self.calls.append(
            (sender_id, message, principal_id, idempotency_key, fingerprint)
        )
        return IntakeResult(
            reply="ack",
            order_id="ord_1",
            decision={"status": "pending_review"},
            duplicate=False,
            idempotent=idempotency_key is not None,
        )


class _Runner:
    def __init__(self):
        self.capabilities = ModelCapabilities()
        self.turns: list[tuple[str, str]] = []

    def run_turn(self, *, sender_id, message, media=None) -> str:
        self.turns.append((sender_id, message))
        return "ack"


class _Orders:
    def __init__(self):
        self.calls: list[tuple] = []

    async def list_orders(self, arguments):
        self.calls.append(("list_orders", arguments))
        return [{"order_id": "ord_1", "status": "approved"}]

    async def get_order(self, arguments):
        self.calls.append(("get_order", arguments))
        return {"order_id": arguments["order_id"], "status": "approved"}

    async def act(self, arguments, principal):
        self.calls.append(("act", arguments, principal.id))
        return {
            "order_id": arguments["order_id"],
            "action": arguments["action"],
            "status": "approved",
            "order": {"order_id": arguments["order_id"]},
        }

    async def voucher_xml(self, arguments, principal):
        self.calls.append(("voucher_xml", arguments, principal.id))
        return "<voucher/>"


class _Loading:
    async def render_loading_list(self, day):
        return {
            "delivery_day": day.isoformat() if day else "2026-11-23",
            "sections": [],
        }

    async def stats(self):
        return {
            "pending_escalations": 2,
            "approved_today": 1,
            "billed_today": 0,
            "late_orders": 0,
        }


async def _server(*, key_role="admin", **overrides):
    auth, admin, operator, approver, viewer = await _auth()
    keys = {
        "admin": admin,
        "operator": operator,
        "approver": approver,
        "viewer": viewer,
        "invalid": "vk_not-a-real-key",
    }
    services = {
        "auth": auth,
        "admin": overrides.get("admin", _Admin()),
        "intake": overrides.get("intake", _Intake()),
        "orders": overrides.get("orders", _Orders()),
        "loading": overrides.get("loading", _Loading()),
    }
    gateway = McpToolGateway(auth, build_mcp_operations(**services))
    server = build_mcp_server(gateway, api_key=keys[key_role])
    return server, services, keys


def _text(result):
    return result.content[0].text


async def _protocol_call(server, name, arguments):
    async with create_client_server_memory_streams() as (client, streams):
        task = asyncio.create_task(
            serve_mcp_streams(server, streams[0], streams[1])
        )
        try:
            async with ClientSession(client[0], client[1]) as session:
                await asyncio.wait_for(session.initialize(), timeout=5)
                return await asyncio.wait_for(
                    session.call_tool(name, arguments), timeout=5
                )
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


async def _direct_call(server, name, arguments, *, headers=None):
    context = SimpleNamespace(headers=headers) if headers is not None else None
    return await server.call_tool(name, arguments, context=context)


def test_every_tool_has_a_rest_equivalent_authorization_policy():
    assert set(MCP_TOOL_NAMES) <= set(DEFAULT_POLICIES)


async def test_operations_cover_exactly_the_registered_tools():
    auth, _, _, _, _ = await _auth()

    operations = build_mcp_operations(auth=auth)

    assert set(operations) == set(MCP_TOOL_NAMES)


def test_operations_are_built_from_services_not_a_store():
    assert "store" not in inspect.signature(build_mcp_operations).parameters


async def test_server_exposes_the_nine_skill_tools():
    server, _, _ = await _server()

    names = {tool.name for tool in await server.list_tools()}

    assert names == set(MCP_TOOL_NAMES)
    assert len(MCP_TOOL_NAMES) == 9


async def test_get_instance_succeeds_through_the_protocol():
    server, _, _ = await _server()

    result = await _protocol_call(server, "get_instance", {})

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body["business_name"] == "Acme Chemicals"
    assert body["role"] == "admin"


async def test_invalid_key_is_rejected_before_any_service_runs():
    intake = _Intake()
    server, _, _ = await _server(key_role="invalid", intake=intake)

    result = await _protocol_call(
        server, "intake", {"sender_id": PHONE, "message": "an order"}
    )

    assert result.is_error is True
    assert "invalid or revoked API key" in _text(result)
    assert intake.calls == []


async def test_role_rules_match_rest():
    server, _, _ = await _server(key_role="viewer")

    result = await _protocol_call(
        server, "intake", {"sender_id": PHONE, "message": "an order"}
    )

    assert result.is_error is True
    assert "role viewer cannot perform this action" in _text(result)


async def test_order_action_requires_confirmation_and_uses_the_caller_action():
    server, services, _ = await _server(key_role="approver")
    orders = services["orders"]

    unconfirmed = await _protocol_call(
        server, "order_action", {"order_id": "ord_1", "action": "approve"}
    )
    assert unconfirmed.is_error is True
    assert "requires confirm: true" in _text(unconfirmed)

    confirmed = await _protocol_call(
        server,
        "order_action",
        {"order_id": "ord_1", "action": "bill", "confirm": True},
    )
    assert confirmed.is_error is False
    assert orders.calls[-1][1]["action"] == "bill"
    assert orders.calls[-1][1]["confirm"] is True


async def test_unknown_tool_is_rejected():
    server, _, _ = await _server()

    result = await _protocol_call(server, "drop_database", {})

    assert result.is_error is True
    assert "Unknown tool" in _text(result)


async def test_intake_uses_the_principal_identity_and_idempotency_key():
    server, services, keys = await _server()
    intake = services["intake"]
    headers = {"Authorization": f"Bearer {keys['operator']}"}

    result = await _direct_call(
        server,
        "intake",
        {
            "sender_id": PHONE,
            "message": "2000 kg sulfuric acid",
            "idempotency_key": "k1",
        },
        headers=headers,
    )

    assert result.is_error is False
    sender, message, principal_id, key, fingerprint = intake.calls[0]
    assert sender == PHONE
    assert message == "2000 kg sulfuric acid"
    assert principal_id
    assert key == "k1"
    assert fingerprint


async def test_http_header_key_overrides_the_process_key():
    server, _, keys = await _server(key_role="invalid")

    with pytest.raises(ToolError):
        await _direct_call(server, "download_voucher", {"order_id": "ord_1"})
    allowed = await _direct_call(
        server,
        "download_voucher",
        {"order_id": "ord_1"},
        headers={"Authorization": f"Bearer {keys['approver']}"},
    )

    assert allowed.is_error is False


async def test_intake_idempotency_matches_the_rest_contract():
    runner = _Runner()
    real_intake = IntakeService(runner, InMemoryOrderStore())
    server, _, keys = await _server(intake=real_intake)
    headers = {"Authorization": f"Bearer {keys['operator']}"}
    arguments = {
        "sender_id": PHONE,
        "message": "2000 kg sulfuric acid",
        "idempotency_key": "retry-1",
    }

    first = await _direct_call(server, "intake", arguments, headers=headers)
    second = await _direct_call(server, "intake", arguments, headers=headers)

    assert json.loads(_text(first))["idempotent"] is False
    assert json.loads(_text(second))["idempotent"] is True

    with pytest.raises(ToolError, match="different payload"):
        await _direct_call(
            server,
            "intake",
            {**arguments, "message": "a different order"},
            headers=headers,
        )
    assert len(runner.turns) == 1


def test_streamable_http_transport_answers_a_real_mcp_call():
    server, _, keys = asyncio.run(_server())
    app = streamable_http_app(
        server,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )
    protocol_headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    with TestClient(app) as client:
        init = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "contract-test", "version": "1"},
                },
            },
            headers=protocol_headers,
        )
        assert init.status_code == 200
        session = init.headers["mcp-session-id"]
        unauthenticated = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "get_instance", "arguments": {}},
            },
            headers={
                **protocol_headers,
                "Mcp-Session-Id": session,
                "MCP-Protocol-Version": "2025-06-18",
            },
        )
        result = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "get_instance", "arguments": {}},
            },
            headers={
                **protocol_headers,
                "Mcp-Session-Id": session,
                "MCP-Protocol-Version": "2025-06-18",
                "Authorization": f"Bearer {keys['admin']}",
            },
        )

    def body(response):
        return json.loads(response.text.split("data: ", 1)[1].splitlines()[0])

    assert unauthenticated.status_code == 200
    assert body(unauthenticated)["result"]["isError"] is True
    denied = body(unauthenticated)["result"]["content"][0]["text"]
    assert "invalid or revoked API key" in denied

    assert result.status_code == 200
    payload = body(result)
    assert payload["result"]["isError"] is False
    assert "Acme Chemicals" in payload["result"]["content"][0]["text"]


def test_both_transports_dispatch_through_the_same_server(monkeypatch):
    server, _, _ = asyncio.run(_server())
    seen = []
    monkeypatch.setattr(
        MCPServer, "run", lambda self, transport, **kwargs: seen.append(transport)
    )

    run_mcp_server(server, transport="stdio")
    run_mcp_server(server, transport="streamable-http", host="0.0.0.0", port=9123)

    assert seen == ["stdio", "streamable-http"]


def test_streamable_http_transport_exposes_an_asgi_app():
    server, _, _ = asyncio.run(_server())

    app = streamable_http_app(server)

    assert isinstance(app, Starlette)
    assert any(getattr(route, "path", "") == "/mcp" for route in app.routes)


def test_cli_mcp_command_selects_the_transport(monkeypatch):
    from src import cli, mcp_app
    from src import mcp_server as mcp_module

    seen = {}
    monkeypatch.setattr(mcp_app, "create_mcp_server", lambda *, api_key: object())
    monkeypatch.setattr(
        mcp_module,
        "run_mcp_server",
        lambda server, **kwargs: seen.update(kwargs),
    )

    code = cli.main(
        [
            "mcp",
            "--transport",
            "streamable-http",
            "--host",
            "0.0.0.0",
            "--port",
            "9123",
            "--api-key",
            "vk_cli-key",
        ]
    )

    assert code == 0
    assert seen == {
        "transport": "streamable-http",
        "host": "0.0.0.0",
        "port": 9123,
    }
