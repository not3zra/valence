"""The Instance view shared by the REST route and the MCP tool (issue #63).

One projection keeps both transports reporting the same profile, role, config,
and Integration health for the same authenticated principal.
"""

from __future__ import annotations

from typing import Any

from .admin import AdminService
from .auth import AuthService, Principal


async def instance_view(
    auth: AuthService, admin: AdminService | None, principal: Principal
) -> dict[str, Any]:
    """Project one Instance as seen by an authenticated caller."""
    profile = await auth.get_instance()
    return {
        "business_name": profile.business_name if profile else "",
        "timezone": profile.timezone if profile else "UTC",
        "role": principal.role.value,
        "principal": principal.id,
        "config": await admin.get_config() if admin else {},
        "integrations": await auth.integration_health(),
    }
