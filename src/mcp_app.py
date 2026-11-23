"""Composition root for the standalone MCP server (issue #63).

Builds the same auth and service layer the REST app uses, then hands them to
:func:`src.mcp_server.build_mcp_operations`. The MCP server is a separate
process from the web app, so nothing here imports the FastAPI app.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from .composition import build_agent_services, build_auth
from .config import settings
from .mcp_auth import McpToolGateway
from .mcp_server import build_mcp_operations, build_mcp_server
from .runtime import build_runtime
from .tally import TallyPushService
from .voucher import default_voucher_storage


def create_mcp_server(*, api_key: str = "") -> MCPServer:
    """Build an MCP server for one Instance from configuration."""
    runtime = build_runtime()
    auth = build_auth(runtime)
    storage = runtime.voucher_storage or default_voucher_storage(
        bucket=settings.voucher_bucket, directory=settings.voucher_dir
    )

    async def configured_tally_url() -> str:
        return await auth.integration_url("tally")

    tally_push = (
        TallyPushService(runtime.store, storage, configured_tally_url)
        if runtime.store is not None
        else None
    )
    services = build_agent_services(runtime, storage, tally_push)
    operations = build_mcp_operations(
        auth=auth,
        admin=services.admin,
        intake=services.intake,
        orders=services.orders,
        loading=services.loading,
    )
    return build_mcp_server(McpToolGateway(auth, operations), api_key=api_key)
