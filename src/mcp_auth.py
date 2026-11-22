"""Authorization gate for MCP tool handlers (issue #57, ADR-0007).

The real MCP transports land in a later ticket (#63). Every handler — whichever
transport it uses — must pass through :class:`McpToolGateway`, which delegates
identity, roles, and ``confirm`` to the same ``AuthService`` the REST routes
use. Handlers receive only their arguments and the authenticated principal;
they are never handed a store or a database session.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any

from .auth import (
    ADMIN_ROLES,
    APPROVER_ROLES,
    CONFIRMED_ACTIONS,
    READ_ROLES,
    AuthService,
    ForbiddenError,
    Principal,
    Role,
    UnauthorizedError,
)
from .order_actions import OrderActionService

Handler = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class ToolPolicy:
    """The REST-equivalent authorization rule for one MCP tool."""

    roles: Collection[Role]
    action: str | None = None


DEFAULT_POLICIES: Mapping[str, ToolPolicy] = {
    "get_instance": ToolPolicy(READ_ROLES),
    "issue_key": ToolPolicy(ADMIN_ROLES),
    "edit_order": ToolPolicy(APPROVER_ROLES),
    "order_action": ToolPolicy(APPROVER_ROLES, action="approve"),
}


def build_order_tool_operations(
    service: OrderActionService,
) -> Mapping[str, Handler]:
    """Expose the shared order service as MCP tool handlers.

    Both handlers already take ``(arguments, principal)``, so MCP and REST run
    the exact same implementation; this gateway supplies the authorization the
    REST router supplies for its routes.
    """
    return {"edit_order": service.edit, "order_action": service.act}


class McpToolGateway:
    """Single authorization entry point for MCP tool calls."""

    def __init__(self, auth: AuthService, operations: Mapping[str, Handler]) -> None:
        self._auth = auth
        self._operations = operations

    async def call(self, tool: str, *, api_key: str, arguments: dict[str, Any]) -> Any:
        policy = DEFAULT_POLICIES.get(tool)
        if policy is None or tool not in self._operations:
            raise ForbiddenError(f"unknown MCP tool {tool!r}")
        principal = await self._auth.authenticate(api_key)
        if principal is None:
            raise UnauthorizedError("invalid or revoked API key")
        await self._authorize(principal, policy, arguments)
        return await self._operations[tool](arguments, principal)

    async def _authorize(
        self, principal: Principal, policy: ToolPolicy, arguments: dict[str, Any]
    ) -> None:
        action = policy.action
        if "action" in arguments:
            action = str(arguments["action"])
            if action not in CONFIRMED_ACTIONS:
                raise ForbiddenError(f"unknown order action {action!r}")
        await self._auth.authorize(
            principal,
            roles=policy.roles,
            action=action,
            confirm=arguments.get("confirm") is True,
        )
