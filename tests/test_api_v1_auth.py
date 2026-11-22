"""REST v1 setup and key authorization use one shared layer (issue #57)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.auth import AuthService, InMemoryAuthStore, Role
from src.web import create_app


def _client(*, setup_token: str = "") -> TestClient:
    auth = AuthService(InMemoryAuthStore(), setup_token=setup_token)
    return TestClient(create_app(auth=auth))


def _setup(client: TestClient, token: str = "setup-secret"):
    return client.post(
        "/api/v1/setup",
        json={"setup_token": token, "business_name": "Acme Chemicals"},
    )


def test_setup_is_closed_when_no_setup_token_is_configured():
    response = _setup(_client(), "")

    assert response.status_code == 503


def test_setup_issues_first_admin_once_then_is_disabled():
    client = _client(setup_token="setup-secret")

    assert _setup(client, "wrong").status_code == 401
    created = _setup(client)
    assert created.status_code == 201
    token = created.json()["token"]
    assert token.startswith("vk_")
    assert _setup(client).status_code == 409
    assert client.get("/api/v1/instance").status_code == 401
    assert (
        client.get(
            "/api/v1/instance", headers={"Authorization": f"Bearer {token}"}
        ).status_code
        == 200
    )


def test_key_routes_enforce_admin_role_server_side():
    client = _client(setup_token="setup-secret")
    admin = _setup(client).json()["token"]
    admin_headers = {"Authorization": f"Bearer {admin}"}
    created = client.post(
        "/api/v1/keys",
        json={"name": "operator", "role": "operator"},
        headers=admin_headers,
    )
    assert created.status_code == 201
    operator_token = created.json()["token"]

    assert (
        client.post(
            "/api/v1/keys",
            json={"name": "other", "role": "viewer"},
            headers={"Authorization": f"Bearer {operator_token}"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/keys", json={"name": "other", "role": "viewer"}
        ).status_code
        == 401
    )
    listed = client.get("/api/v1/keys", headers=admin_headers)
    assert listed.status_code == 200
    assert all("key_hash" not in key and "token" not in key for key in listed.json())


def test_revoking_a_key_through_rest_immediately_blocks_it():
    client = _client(setup_token="setup-secret")
    admin = _setup(client).json()["token"]
    admin_headers = {"Authorization": f"Bearer {admin}"}
    key = client.post(
        "/api/v1/keys",
        json={"name": "viewer", "role": "viewer"},
        headers=admin_headers,
    ).json()
    viewer_headers = {"Authorization": f"Bearer {key['token']}"}
    assert client.get("/api/v1/instance", headers=viewer_headers).status_code == 200

    deleted = client.delete(f"/api/v1/keys/{key['key_id']}", headers=admin_headers)
    assert deleted.status_code == 204
    assert client.get("/api/v1/instance", headers=viewer_headers).status_code == 401


def test_instance_route_reports_the_authenticated_principal():
    client = _client(setup_token="setup-secret")
    admin = _setup(client).json()["token"]

    body = client.get(
        "/api/v1/instance", headers={"Authorization": f"Bearer {admin}"}
    ).json()

    assert body["business_name"] == "Acme Chemicals"
    assert body["role"] == Role.ADMIN.value
    assert "principal" in body


@pytest.mark.parametrize("role", [Role.ADMIN, Role.OPERATOR, Role.VIEWER])
def test_read_roles_can_read_instance(role):
    client = _client(setup_token="setup-secret")
    admin = _setup(client).json()["token"]
    issued = client.post(
        "/api/v1/keys",
        json={"name": role.value, "role": role.value},
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert issued.status_code == 201
    assert (
        client.get(
            "/api/v1/instance",
            headers={"Authorization": f"Bearer {issued.json()['token']}"},
        ).status_code
        == 200
    )
