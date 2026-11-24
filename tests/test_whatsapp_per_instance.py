"""Per-instance Meta credentials on the WhatsApp webhook (issue #65).

When an Agent Interface is mounted, the webhook's verify token, app secret,
access token and phone number id come from this Instance's stored Meta
Integration — not from process-level env settings. The handshake, signature,
media fetch and confirmation reply all use that one set, and a channel with no
stored credentials fails closed.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from google.adk.sessions import InMemorySessionService

from src.agent import build_agent
from src.auth import AuthService, InMemoryAuthStore
from src.media import MediaObject
from src.meta_whatsapp import MetaCredentials, MetaWhatsAppSender, build_meta_signature
from src.store import InMemoryOrderStore
from src.web import create_app
from src.whatsapp import MockWhatsAppSender

from .fakes import ConfirmingToolCallingLlm, PhotoReadingLlm

SPEC_PATH = "/api/v1/webhooks/whatsapp"


def _creds(**overrides) -> dict[str, str]:
    return {
        "app_secret": "instance-app-secret",
        "verify_token": "instance-verify-token",
        "access_token": "instance-access-token",
        "phone_number_id": "instance-phone-id",
        **overrides,
    }


def _client(auth: AuthService, *, sender=None, model=None):
    store = InMemoryOrderStore()
    app = create_app(
        agent=build_agent(model=model or ConfirmingToolCallingLlm(), store=store),
        session_service=InMemorySessionService(),
        whatsapp_sender=sender,
        auth=auth,
    )
    return TestClient(app), sender, store


def _payload(body: str, sender: str = "919812345001") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WEBHOOK_BUSINESS_ACCOUNT_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "1"},
                            "messages": [
                                {
                                    "from": sender,
                                    "id": "wamid.per_instance",
                                    "timestamp": "1700000000",
                                    "type": "text",
                                    "text": {"body": body},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def _image_payload() -> dict:
    payload = _payload("")
    message = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    message["type"] = "image"
    message["image"] = {"id": "1234567890", "mime_type": "image/jpeg"}
    del message["text"]
    return payload


def _post(client, path, payload, secret):
    body = json.dumps(payload).encode()
    return client.post(
        path,
        content=body,
        headers={
            "content-type": "application/json",
            "X-Hub-Signature-256": build_meta_signature(body, secret),
        },
    )


def test_credentials_never_appear_in_repr():
    credentials = MetaCredentials(
        app_secret="APP-SECRET-VALUE",
        verify_token="VERIFY-TOKEN-VALUE",
        access_token="ACCESS-TOKEN-VALUE",
        phone_number_id="PHONE-ID-VALUE",
    )
    text = repr(credentials)
    assert "APP-SECRET-VALUE" not in text
    assert "VERIFY-TOKEN-VALUE" not in text
    assert "ACCESS-TOKEN-VALUE" not in text
    assert "PHONE-ID-VALUE" not in text


async def test_handshake_uses_the_instance_verify_token():
    auth = AuthService(InMemoryAuthStore())
    await auth.set_integration("meta", _creds())
    client, _, _ = _client(auth, sender=MockWhatsAppSender())

    good = client.get(
        SPEC_PATH,
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "instance-verify-token",
            "hub.challenge": "challenge-abc",
        },
    )
    assert good.status_code == 200
    assert good.text == "challenge-abc"

    wrong = client.get(
        SPEC_PATH,
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "some-other-token",
            "hub.challenge": "challenge-abc",
        },
    )
    assert wrong.status_code == 403


async def test_signature_uses_the_instance_app_secret_and_fails_closed():
    auth = AuthService(InMemoryAuthStore())
    await auth.set_integration("meta", _creds())
    client, _, store = _client(auth, sender=MockWhatsAppSender())
    payload = _payload("2 drums sulfuric acid chahiye")

    assert _post(client, SPEC_PATH, payload, "instance-app-secret").status_code == 200
    assert len(store.orders) == 1

    before = len(store.orders)
    assert _post(client, SPEC_PATH, payload, "wrong-secret").status_code == 403
    assert len(store.orders) == before


async def test_reply_uses_the_instance_access_token_and_phone_number(monkeypatch):
    captured: dict[str, str] = {}

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self) -> bytes:
            return b"{}"

    def fake_open(self, request):
        captured["url"] = request.full_url
        captured["authorization"] = request.headers.get("Authorization", "")
        return _Response()

    monkeypatch.setattr(MetaWhatsAppSender, "_default_open", fake_open)

    auth = AuthService(InMemoryAuthStore())
    await auth.set_integration("meta", _creds())
    client, _, store = _client(auth)

    response = _post(
        client, SPEC_PATH, _payload("2 drums sulfuric acid chahiye"),
        "instance-app-secret",
    )
    assert response.status_code == 200
    assert len(store.orders) == 1
    assert captured["authorization"] == "Bearer instance-access-token"
    assert "/instance-phone-id/messages" in captured["url"]


async def test_media_fetch_uses_the_instance_access_token(monkeypatch):
    captured: dict[str, str] = {}

    class _CapturingFetcher:
        def __init__(self, token: str) -> None:
            captured["token"] = token

        def fetch(self, reference: str) -> MediaObject:
            return MediaObject(data=b"\xff\xd8fake-jpeg", mime_type="image/jpeg")

    monkeypatch.setattr("src.web.MetaMediaFetcher", _CapturingFetcher)

    auth = AuthService(InMemoryAuthStore())
    await auth.set_integration("meta", _creds())
    client, _, store = _client(
        auth, sender=MockWhatsAppSender(), model=PhotoReadingLlm()
    )

    response = _post(client, SPEC_PATH, _image_payload(), "instance-app-secret")
    assert response.status_code == 200
    assert captured["token"] == "instance-access-token"
    assert len(store.orders) == 1
    assert store.orders[0].source_channel == "photo"


async def test_two_instances_have_independent_credentials():
    auth_a = AuthService(InMemoryAuthStore())
    await auth_a.set_integration(
        "meta", _creds(app_secret="secret-a", verify_token="token-a")
    )
    auth_b = AuthService(InMemoryAuthStore())
    await auth_b.set_integration(
        "meta", _creds(app_secret="secret-b", verify_token="token-b")
    )
    client_a, _, store_a = _client(auth_a, sender=MockWhatsAppSender())
    client_b, _, store_b = _client(auth_b, sender=MockWhatsAppSender())

    def handshake(client, token):
        return client.get(
            SPEC_PATH,
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": token,
                "hub.challenge": "abc",
            },
        ).status_code

    assert handshake(client_a, "token-a") == 200
    assert handshake(client_a, "token-b") == 403
    assert handshake(client_b, "token-b") == 200
    assert handshake(client_b, "token-a") == 403

    payload = _payload("2 drums sulfuric acid chahiye")
    assert _post(client_a, SPEC_PATH, payload, "secret-a").status_code == 200
    assert _post(client_b, SPEC_PATH, payload, "secret-a").status_code == 403
    assert len(store_a.orders) == 1
    assert store_b.orders == []


async def test_webhook_fails_closed_without_stored_credentials():
    auth = AuthService(InMemoryAuthStore())
    client, _, store = _client(auth, sender=MockWhatsAppSender())

    handshake = client.get(
        SPEC_PATH,
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "anything",
            "hub.challenge": "abc",
        },
    )
    assert handshake.status_code == 403

    post = _post(client, SPEC_PATH, _payload("Namaste"), "anything")
    assert post.status_code == 403
    assert store.orders == []


async def test_integration_secrets_are_not_exposed_and_keys_cannot_sign():
    auth = AuthService(InMemoryAuthStore())
    creds = _creds()
    await auth.set_integration("meta", creds)
    admin = await auth.issue_admin_key(name="admin")
    client, _, store = _client(auth, sender=MockWhatsAppSender())

    for path in ("/api/v1/integrations", "/api/v1/instance"):
        response = client.get(path, headers={"Authorization": f"Bearer {admin.token}"})
        assert response.status_code == 200
        assert creds["app_secret"] not in response.text
        assert creds["access_token"] not in response.text

    # A user API key is not a webhook credential: signing with it is rejected.
    payload = _payload("2 drums sulfuric acid chahiye")
    response = _post(client, SPEC_PATH, payload, admin.token)
    assert response.status_code == 403
    assert store.orders == []
