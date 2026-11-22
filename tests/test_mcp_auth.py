"""MCP handlers share REST's authorization; there is no bypass path (issue #57)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.auth import (
    AuthService,
    ConfirmationRequired,
    ForbiddenError,
    InMemoryAuthStore,
    Role,
    UnauthorizedError,
)
from src.mcp_auth import McpToolGateway
from src.web import create_app


class _Operations:
    def __init__(self):
        self.calls = []

    async def issue_key(self, arguments, principal):
        self.calls.append(("issue_key", arguments, principal))
        return {"issued": arguments["name"]}

    async def order_action(self, arguments, principal):
        self.calls.append(("order_action", arguments, principal))
        return {"approved": arguments["order_id"]}

    async def get_instance(self, arguments, principal):
        self.calls.append(("get_instance", arguments, principal))
        return {"role": principal.role.value}

    def as_mapping(self):
        return {
            "issue_key": self.issue_key,
            "order_action": self.order_action,
            "get_instance": self.get_instance,
        }


async def _auth():
    service = AuthService(
        InMemoryAuthStore(), approver_phones=lambda: _async(["+919812345001"])
    )
    admin = await service.issue_admin_key(name="first-admin")
    viewer = await service.issue_key(name="viewer", role=Role.VIEWER)
    approver = await service.issue_key(
        name="approver", role=Role.APPROVER, phone="+919812345001"
    )
    return service, admin.token, viewer.token, approver.token


def _gateway(auth, operations):
    return McpToolGateway(auth, operations.as_mapping())


async def test_gateway_enforces_the_same_roles_as_rest():
    auth, admin, viewer, _ = await _auth()
    operations = _Operations()
    gateway = _gateway(auth, operations)

    with pytest.raises(ForbiddenError):
        await gateway.call("issue_key", api_key=viewer, arguments={"name": "x"})
    result = await gateway.call(
        "issue_key", api_key=admin, arguments={"name": "operator"}
    )
    assert result == {"issued": "operator"}
    assert operations.calls[0][2].id


async def test_gateway_requires_confirm_for_privileged_actions():
    auth, _, _, approver = await _auth()
    gateway = _gateway(auth, _Operations())

    with pytest.raises(ConfirmationRequired):
        await gateway.call(
            "order_action", api_key=approver, arguments={"order_id": "o-1"}
        )
    assert await gateway.call(
        "order_action",
        api_key=approver,
        arguments={"order_id": "o-1", "confirm": True},
    ) == {"approved": "o-1"}


async def test_gateway_uses_the_callers_action_not_a_hardcoded_one():
    auth, _, _, approver = await _auth()
    gateway = _gateway(auth, _Operations())

    with pytest.raises(ConfirmationRequired):
        await gateway.call(
            "order_action",
            api_key=approver,
            arguments={"order_id": "o-1", "action": "bill"},
        )
    with pytest.raises(ForbiddenError):
        await gateway.call(
            "order_action",
            api_key=approver,
            arguments={"order_id": "o-1", "action": "drop_table"},
        )
    with pytest.raises(ConfirmationRequired):
        await gateway.call(
            "order_action",
            api_key=approver,
            arguments={"order_id": "o-1", "action": "dispatch"},
        )
    with pytest.raises(ConfirmationRequired):
        await gateway.call(
            "order_action",
            api_key=approver,
            arguments={"order_id": "o-1", "action": "bill", "confirm": "false"},
        )
    assert await gateway.call(
        "order_action",
        api_key=approver,
        arguments={"order_id": "o-1", "action": "bill", "confirm": True},
    ) == {"approved": "o-1"}


async def test_key_revoked_through_rest_is_rejected_by_mcp():
    auth, admin, viewer, _ = await _auth()
    client = TestClient(create_app(auth=auth))
    viewer_key = [key for key in await auth.list_keys() if key.role is Role.VIEWER][0]
    assert (
        client.delete(
            f"/api/v1/keys/{viewer_key.id}",
            headers={"Authorization": f"Bearer {admin}"},
        ).status_code
        == 204
    )
    gateway = _gateway(auth, _Operations())

    with pytest.raises(UnauthorizedError):
        await gateway.call("get_instance", api_key=viewer, arguments={})


async def test_gateway_rejects_unknown_tools_and_missing_keys():
    auth, _, viewer, _ = await _auth()
    operations = _Operations()
    gateway = _gateway(auth, operations)

    with pytest.raises(UnauthorizedError):
        await gateway.call("get_instance", api_key="", arguments={})
    with pytest.raises(ForbiddenError):
        await gateway.call("drop_database", api_key=viewer, arguments={})
    assert operations.calls == []


async def _async(value):
    return value
