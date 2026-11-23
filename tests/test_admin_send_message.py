"""The admin-authenticated outbound send endpoint (security finding #74).

Outbound WhatsApp is a privileged action: it requires a server-side admin key,
not the shared voice-ingest bearer. The legacy probe is gone; this module pins
the v1 contract, its body cap (carried over from #75), and its error shapes.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from google.adk.sessions import InMemorySessionService

from src.agent import build_agent
from src.api_v1 import MAX_ADMIN_SEND_MESSAGE_BODY_BYTES
from src.auth import AuthService, InMemoryAuthStore, Role
from src.web import create_app
from src.whatsapp import MockWhatsAppSender

from .fakes import FakeEchoLlm

SEND_URL = "/api/v1/admin/send-message"
SETUP_URL = "/api/v1/setup"
SETUP_TOKEN = "setup-secret"
INGEST_TOKEN = "ingest-token"


def _client(*, sender=None) -> TestClient:
    app = create_app(
        agent=build_agent(model=FakeEchoLlm()),
        session_service=InMemorySessionService(),
        auth=AuthService(InMemoryAuthStore(), setup_token=SETUP_TOKEN),
        whatsapp_sender=sender or MockWhatsAppSender(),
        voice_ingest_token=INGEST_TOKEN,
    )
    return TestClient(app)


def _admin(client: TestClient) -> dict[str, str]:
    response = client.post(
        SETUP_URL, json={"setup_token": SETUP_TOKEN, "business_name": "Acme"}
    )
    assert response.status_code == 201
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _key_headers(
    client: TestClient, admin: dict[str, str], role: Role
) -> dict[str, str]:
    created = client.post(
        "/api/v1/keys",
        json={"name": role.value, "role": role.value},
        headers=admin,
    )
    assert created.status_code == 201
    return {"Authorization": f"Bearer {created.json()['token']}"}


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_admin_key_can_send_a_message():
    sender = MockWhatsAppSender()
    client = _client(sender=sender)
    response = client.post(
        SEND_URL,
        json={"to": "+919812345001", "text": "hello"},
        headers=_admin(client),
    )
    assert response.status_code == 200
    assert sender.sent == [("+919812345001", "hello")]


def test_voice_ingest_bearer_is_rejected():
    client = _client()
    _admin(client)
    response = client.post(
        SEND_URL,
        json={"to": "+919812345001", "text": "hello"},
        headers=_auth(INGEST_TOKEN),
    )
    assert response.status_code == 401


def test_non_admin_roles_are_forbidden():
    client = _client()
    admin = _admin(client)
    for role in (Role.OPERATOR, Role.VIEWER):
        response = client.post(
            SEND_URL,
            json={"to": "+919812345001", "text": "hello"},
            headers=_key_headers(client, admin, role),
        )
        assert response.status_code == 403


def test_revoked_admin_key_is_rejected():
    client = _client()
    admin_headers = _admin(client)
    created = client.post(
        "/api/v1/keys",
        json={"name": "second-admin", "role": Role.ADMIN.value},
        headers=admin_headers,
    )
    assert created.status_code == 201
    second = created.json()
    second_headers = {"Authorization": f"Bearer {second['token']}"}
    assert (
        client.post(
            SEND_URL,
            json={"to": "+919812345001", "text": "hello"},
            headers=second_headers,
        ).status_code
        == 200
    )
    deleted = client.delete(
        f"/api/v1/keys/{second['key_id']}", headers=admin_headers
    )
    assert deleted.status_code == 204
    assert (
        client.post(
            SEND_URL,
            json={"to": "+919812345001", "text": "hello"},
            headers=second_headers,
        ).status_code
        == 401
    )


def test_oversized_body_is_rejected_after_auth():
    client = _client()
    admin = _admin(client)
    response = client.post(
        SEND_URL,
        content=b"x" * (MAX_ADMIN_SEND_MESSAGE_BODY_BYTES + 1),
        headers={"content-type": "application/json", **admin},
    )
    assert response.status_code == 413


def test_oversized_body_with_bad_token_is_401():
    client = _client()
    _admin(client)
    response = client.post(
        SEND_URL,
        content=b"x" * (MAX_ADMIN_SEND_MESSAGE_BODY_BYTES + 1),
        headers={"content-type": "application/json", **_auth("nope")},
    )
    assert response.status_code == 401


def test_legacy_route_is_gone():
    client = _client()
    _admin(client)
    assert client.post(
        "/api/admin/send-message",
        json={"to": "+919812345001", "text": "hello"},
        headers=_auth(INGEST_TOKEN),
    ).status_code == 404


def test_invalid_recipient_and_missing_fields_are_422():
    client = _client()
    admin = _admin(client)
    invalid = client.post(
        SEND_URL, json={"to": "not-a-phone", "text": "hello"}, headers=admin
    )
    assert invalid.status_code == 422
    missing = client.post(
        SEND_URL, json={"to": "+919812345001"}, headers=admin
    )
    assert missing.status_code == 422


def test_malformed_and_non_object_json_are_400():
    client = _client()
    admin = _admin(client)
    malformed = client.post(
        SEND_URL,
        content=b"{",
        headers={"content-type": "application/json", **admin},
    )
    assert malformed.status_code == 400
    non_object = client.post(
        SEND_URL,
        content=b"[]",
        headers={"content-type": "application/json", **admin},
    )
    assert non_object.status_code == 400
