"""The token-gated outbound send probe (security findings #75 and #74).

The legacy probe is being retired in favour of the admin-authenticated v1
endpoint; while it exists its request body must be bounded like the other
token-gated ingest routes, and its error responses must be real HTTP errors.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from google.adk.sessions import InMemorySessionService

from src.agent import build_agent
from src.web import MAX_ADMIN_SEND_MESSAGE_BODY_BYTES, create_app
from src.whatsapp import MockWhatsAppSender

from .fakes import FakeEchoLlm

SEND_URL = "/api/admin/send-message"
TOKEN = "ingest-token"


def _client(*, sender=None, token=TOKEN) -> TestClient:
    app = create_app(
        agent=build_agent(model=FakeEchoLlm()),
        session_service=InMemorySessionService(),
        whatsapp_sender=sender or MockWhatsAppSender(),
        voice_ingest_token=token,
    )
    return TestClient(app)


def _auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_send_message_delivers_for_valid_token():
    sender = MockWhatsAppSender()
    response = _client(sender=sender).post(
        SEND_URL,
        json={"to": "+919812345001", "text": "hello"},
        headers=_auth(),
    )
    assert response.status_code == 200
    assert sender.sent == [("+919812345001", "hello")]


def test_send_message_rejects_oversized_body_with_valid_token():
    response = _client().post(
        SEND_URL,
        content=b"x" * (MAX_ADMIN_SEND_MESSAGE_BODY_BYTES + 1),
        headers={"content-type": "application/json", **_auth()},
    )
    assert response.status_code == 413


def test_send_message_checks_token_before_reading_the_body():
    response = _client().post(
        SEND_URL,
        content=b"x" * (MAX_ADMIN_SEND_MESSAGE_BODY_BYTES + 1),
        headers={"content-type": "application/json", **_auth("nope")},
    )
    assert response.status_code == 401


def test_send_message_accepts_a_body_at_the_cap():
    text = "x" * 200
    payload = f'{{"to":"+919812345001","text":"{text}"}}'.encode()
    assert len(payload) < MAX_ADMIN_SEND_MESSAGE_BODY_BYTES
    response = _client().post(
        SEND_URL,
        content=payload,
        headers={"content-type": "application/json", **_auth()},
    )
    assert response.status_code == 200


def test_send_message_returns_400_for_missing_fields():
    response = _client().post(
        SEND_URL, json={"to": "+919812345001"}, headers=_auth()
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "missing 'to' or 'text'"


def test_send_message_returns_503_when_not_configured(monkeypatch):
    from src.config import settings

    monkeypatch.delenv("VOICE_INGEST_TOKEN", raising=False)
    monkeypatch.setattr(settings, "voice_ingest_token", "")
    response = _client(token=None).post(
        SEND_URL,
        json={"to": "+919812345001", "text": "hello"},
        headers=_auth(),
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "not configured"


def test_send_message_rejects_malformed_and_non_object_json():
    client = _client()
    malformed = client.post(
        SEND_URL, content=b"{", headers={"content-type": "application/json", **_auth()}
    )
    assert malformed.status_code == 400
    non_object = client.post(
        SEND_URL, content=b"[]", headers={"content-type": "application/json", **_auth()}
    )
    assert non_object.status_code == 400
