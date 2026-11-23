"""MCP server exposing the nine Skill-facing operations (issue #63, ADR-0007).

MCP is a transport adapter, not a second implementation of the Agent Interface.
Every tool resolves a key from the transport (an ``Authorization`` header over
streamable HTTP, the configured key over stdio), then calls
:class:`~src.mcp_auth.McpToolGateway`. Handlers receive only arguments and the
authenticated principal; they reach services, never a store or a database
session. The same ``MCPServer`` serves both transports, so a tool cannot behave
differently depending on how a client connected.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.lowlevel.server import Server as LowLevelServer
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from .admin import AdminService
from .auth import AuthError, AuthService, bearer_token
from .composition import AgentServices
from .core import ApprovalError
from .instance_service import instance_view
from .intake import (
    IdempotencyConflict,
    IntakeService,
    intake_fingerprint,
    validate_idempotency_key,
    validate_intake_message,
    validate_intake_sender,
)
from .loading_service import LoadingService, parse_delivery_day
from .mcp_auth import MCP_TOOL_POLICIES, Handler, McpToolGateway
from .order_actions import OrderActionService
from .tally import TallyPushError
from .voucher import VoucherError

MCP_TOOL_NAMES = tuple(MCP_TOOL_POLICIES)

_TOOL_ERRORS = (
    AuthError,
    ValueError,
    ApprovalError,
    IdempotencyConflict,
    TallyPushError,
    VoucherError,
    TimeoutError,
    asyncio.TimeoutError,
)


def build_mcp_operations(
    *,
    auth: AuthService,
    admin: AdminService | None = None,
    intake: IntakeService | None = None,
    orders: OrderActionService | None = None,
    loading: LoadingService | None = None,
) -> Mapping[str, Handler]:
    """Adapt the shared services into ``(arguments, principal)`` tool handlers."""

    async def get_instance(arguments: Mapping[str, Any], principal) -> dict[str, Any]:
        return await instance_view(auth, admin, principal)

    async def intake_order(arguments: Mapping[str, Any], principal) -> dict[str, Any]:
        if intake is None:
            raise ValueError("intake is not configured")
        sender_id = validate_intake_sender(str(arguments["sender_id"]))
        message = validate_intake_message(str(arguments.get("message", "")))
        key = validate_idempotency_key(arguments.get("idempotency_key"))
        result = await intake.intake_text(
            sender_id=sender_id,
            message=message,
            principal_id=principal.id,
            idempotency_key=key,
            fingerprint=intake_fingerprint(
                {"sender_id": sender_id, "message": message}
            ),
        )
        return result.to_dict()

    async def list_orders(
        arguments: Mapping[str, Any], principal
    ) -> list[dict[str, Any]]:
        if orders is None:
            raise ValueError("order service is not configured")
        return await orders.list_orders(arguments)

    async def get_order(arguments: Mapping[str, Any], principal) -> dict[str, Any]:
        if orders is None:
            raise ValueError("order service is not configured")
        return await orders.get_order(arguments)

    async def order_action(arguments: Mapping[str, Any], principal) -> dict[str, Any]:
        if orders is None:
            raise ValueError("order service is not configured")
        return await orders.act(arguments, principal)

    async def download_voucher(arguments: Mapping[str, Any], principal) -> str:
        if orders is None:
            raise ValueError("order service is not configured")
        return await orders.voucher_xml(arguments, principal)

    async def list_loading_lists(
        arguments: Mapping[str, Any], principal
    ) -> dict[str, Any]:
        if loading is None:
            raise ValueError("loading service is not configured")
        day = arguments.get("day")
        return await loading.render_loading_list(parse_delivery_day(day))

    async def get_stats(arguments: Mapping[str, Any], principal) -> dict[str, Any]:
        if loading is None:
            raise ValueError("loading service is not configured")
        return await loading.stats()

    async def list_catalog(
        arguments: Mapping[str, Any], principal
    ) -> list[dict[str, Any]]:
        if admin is None:
            raise ValueError("catalogue is not configured")
        return await admin.list_catalog(str(arguments["kind"]))

    return {
        "get_instance": get_instance,
        "intake": intake_order,
        "list_orders": list_orders,
        "get_order": get_order,
        "order_action": order_action,
        "download_voucher": download_voucher,
        "list_loading_lists": list_loading_lists,
        "get_stats": get_stats,
        "list_catalog": list_catalog,
    }


def build_mcp_server(
    gateway: McpToolGateway, *, api_key: str = "", name: str = "valence"
) -> MCPServer:
    """Register the nine tools on one server shared by both transports."""

    server = MCPServer(
        name=name,
        title="Valence Agent Interface",
        instructions=(
            "Operate one Valence Instance. Every call is authorized by the "
            "caller's API key; privileged order actions require confirm: true."
        ),
    )

    def credential(context: Context | None) -> str:
        try:
            headers = context.headers if context is not None else None
        except (AttributeError, ValueError):
            headers = None
        if headers is None:
            return api_key
        for header, value in headers.items():
            if str(header).lower() == "authorization":
                return bearer_token(value) or ""
        return ""

    async def call(
        tool: str, arguments: Mapping[str, Any], context: Context | None
    ) -> Any:
        try:
            return await gateway.call(
                tool, api_key=credential(context), arguments=dict(arguments)
            )
        except _TOOL_ERRORS as error:
            raise ToolError(str(error)) from error

    @server.tool(name="get_instance", description="Instance profile, role and config")
    async def get_instance(ctx: Context) -> dict[str, Any]:
        return await call("get_instance", {}, ctx)

    @server.tool(name="intake", description="Place an order from natural-language text")
    async def intake(
        sender_id: str,
        message: str,
        idempotency_key: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        arguments = {"sender_id": sender_id, "message": message}
        if idempotency_key is not None:
            arguments["idempotency_key"] = idempotency_key
        return await call("intake", arguments, ctx)

    @server.tool(name="list_orders", description="List and filter orders")
    async def list_orders(
        status: str | None = None,
        customer: str | None = None,
        phone: str | None = None,
        date: str | None = None,
        ctx: Context | None = None,
    ) -> list[dict[str, Any]]:
        arguments = {
            key: value
            for key, value in {
                "status": status,
                "customer": customer,
                "phone": phone,
                "date": date,
            }.items()
            if value is not None
        }
        return await call("list_orders", arguments, ctx)

    @server.tool(name="get_order", description="One order with its event timeline")
    async def get_order(order_id: str, ctx: Context | None = None) -> dict[str, Any]:
        return await call("get_order", {"order_id": order_id}, ctx)

    @server.tool(
        name="order_action",
        description=(
            "Decide an order; privileged actions require confirm: true. "
            "An idempotency key makes a retry safe."
        ),
    )
    async def order_action(
        order_id: str,
        action: str,
        confirm: bool = False,
        idempotency_key: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        arguments = {"order_id": order_id, "action": action, "confirm": confirm}
        if idempotency_key is not None:
            arguments["idempotency_key"] = idempotency_key
        return await call("order_action", arguments, ctx)

    @server.tool(name="download_voucher", description="Read a prepared voucher's XML")
    async def download_voucher(order_id: str, ctx: Context | None = None) -> str:
        return await call("download_voucher", {"order_id": order_id}, ctx)

    @server.tool(
        name="list_loading_lists", description="Render a delivery-day Loading List"
    )
    async def list_loading_lists(
        day: str | None = None, ctx: Context | None = None
    ) -> dict[str, Any]:
        arguments = {"day": day} if day is not None else {}
        return await call("list_loading_lists", arguments, ctx)

    @server.tool(name="get_stats", description="Live approval and billing counts")
    async def get_stats(ctx: Context | None = None) -> dict[str, Any]:
        return await call("get_stats", {}, ctx)

    @server.tool(name="list_catalog", description="Read a catalogue collection")
    async def list_catalog(
        kind: str, ctx: Context | None = None
    ) -> list[dict[str, Any]]:
        return await call("list_catalog", {"kind": kind}, ctx)

    return server


def build_mcp_server_for_services(
    auth: AuthService, services: AgentServices, *, api_key: str = ""
) -> MCPServer:
    """Build one MCP server over the services the REST app already uses.

    The hosted app passes no process key: every HTTP call must carry its own
    Bearer key. The stdio CLI passes the configured key for its single caller.
    """
    operations = build_mcp_operations(
        auth=auth,
        admin=services.admin,
        intake=services.intake,
        orders=services.orders,
        loading=services.loading,
    )
    return build_mcp_server(McpToolGateway(auth, operations), api_key=api_key)


def mounted_mcp_app(server: MCPServer) -> Starlette:
    """Build the streamable HTTP app for the main app's ``/mcp`` route.

    The caller's Bearer key is the authorization gate for every tool, as it is
    for REST; host allowlisting is left to the Instance's reverse proxy rather
    than rejecting legitimate deployment domains here.
    """
    return streamable_http_app(
        server,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )


@asynccontextmanager
async def mcp_lifespan(app: Starlette) -> AsyncIterator[None]:
    """Run the MCP session manager while the main app is alive.

    Starlette does not propagate a mounted app's lifespan to its parent, so the
    main app enters it explicitly instead.
    """
    async with app.router.lifespan_context(app):
        yield


async def serve_mcp_streams(server: MCPServer, read_stream, write_stream) -> None:
    """Drive one MCP session over a pair of streams (used by both transports)."""
    lowlevel: LowLevelServer = server._lowlevel_server
    await lowlevel.run(
        read_stream, write_stream, lowlevel.create_initialization_options()
    )


def streamable_http_app(server: MCPServer, **kwargs: Any) -> Starlette:
    """Build the ASGI app for the ``streamable-http`` transport."""
    return server.streamable_http_app(**kwargs)


def run_mcp_server(
    server: MCPServer,
    *,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8000,
    path: str = "/mcp",
) -> None:
    """Run the server over ``stdio`` or ``streamable-http``."""
    if transport == "stdio":
        server.run(transport="stdio")
    elif transport == "streamable-http":
        server.run(
            transport="streamable-http",
            host=host,
            port=port,
            streamable_http_path=path,
        )
    else:
        raise ValueError(f"unsupported MCP transport {transport!r}")
