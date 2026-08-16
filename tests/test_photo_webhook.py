"""Photo intake over the Meta WhatsApp webhook (issue #11, swapped to Meta #13).

A webhook message carrying a Meta media id fetches the photo through the
``MediaFetcher`` seam (now the Graph API with a bearer token), passes it to the
ADK agent as an inline image, and the agent reads it into the same structured
order shape as text — flowing through the Order Processing Core exactly like a
text order. A photo whose handwriting cannot be read falls back to the
escalation path rather than a guessed order.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from google.adk.sessions import InMemorySessionService

from src.agent import build_agent
from src.media import MediaObject
from src.meta_whatsapp import build_meta_signature
from src.store import InMemoryOrderStore
from src.web import create_app
from src.whatsapp import MockWhatsAppSender

from .fakes import FakeMediaFetcher, PhotoReadingLlm, UnreadablePhotoLlm

WEBHOOK_URL = "/api/whatsapp/webhook"
APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"
MEDIA_ID = "1234567890"


def _payload(message: dict) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WHATSAPP_BUSINESS_ACCOUNT_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "16505551111",
                                "phone_number_id": "123456789012345",
                            },
                            "messages": [message],
                        },
                    }
                ],
            }
        ],
    }


def _image_message(media_id: str = MEDIA_ID) -> dict:
    return {
        "from": "919812345001",
        "id": "wamid.photo",
        "timestamp": "1700000000",
        "type": "image",
        "image": {"id": media_id, "mime_type": "image/jpeg", "sha256": "abcd"},
    }


def _sign(payload: dict) -> str:
    return build_meta_signature(json.dumps(payload).encode(), APP_SECRET)


def _post(client, payload, headers=None):
    """POST the exact bytes Meta would send, signed over those bytes."""
    body = json.dumps(payload).encode()
    hdrs = {"content-type": "application/json"}
    if headers is not None:
        hdrs.update(headers)
    else:
        hdrs["X-Hub-Signature-256"] = _sign(payload)
    return client.post(WEBHOOK_URL, content=body, headers=hdrs)


def test_photo_webhook_reads_order_and_confirms():
    sender = MockWhatsAppSender()
    store = InMemoryOrderStore()
    fetcher = FakeMediaFetcher(
        media=MediaObject(data=b"\xff\xd8fake-jpeg", mime_type="image/jpeg")
    )
    app = create_app(
        agent=build_agent(model=PhotoReadingLlm(), store=store),
        session_service=InMemorySessionService(),
        whatsapp_sender=sender,
        media_fetcher=fetcher,
        meta_app_secret=APP_SECRET,
        meta_verify_token=VERIFY_TOKEN,
    )
    client = TestClient(app)

    payload = _payload(_image_message())
    response = _post(client, payload)
    assert response.status_code == 200

    assert fetcher.requested_refs == [MEDIA_ID]
    assert len(store.orders) == 1
    order = store.orders[0]
    assert order.status.value == "approved"
    assert order.source_channel == "photo"

    assert sender.sent == [
        (
            "+919812345001",
            "Order confirmed. Estimated total: 35,000 INR.",
        )
    ]


def test_photo_webhook_unreadable_handwriting_escalates():
    sender = MockWhatsAppSender()
    store = InMemoryOrderStore()
    fetcher = FakeMediaFetcher(
        media=MediaObject(data=b"blurry", mime_type="image/jpeg")
    )
    app = create_app(
        agent=build_agent(model=UnreadablePhotoLlm(), store=store),
        session_service=InMemorySessionService(),
        whatsapp_sender=sender,
        media_fetcher=fetcher,
        meta_app_secret=APP_SECRET,
        meta_verify_token=VERIFY_TOKEN,
    )
    client = TestClient(app)

    payload = _payload(_image_message())
    response = _post(client, payload)
    assert response.status_code == 200

    assert len(store.orders) == 1
    order = store.orders[0]
    assert order.status.value == "pending_review"
    assert "missing_field" in order.escalation_reasons
    assert "low_confidence" in order.escalation_reasons
    assert "under approval" in sender.sent[0][1]


def test_photo_webhook_media_fetch_failure_falls_back_to_text():
    sender = MockWhatsAppSender()
    store = InMemoryOrderStore()
    fetcher = FakeMediaFetcher(media=None)
    app = create_app(
        agent=build_agent(model=PhotoReadingLlm(), store=store),
        session_service=InMemorySessionService(),
        whatsapp_sender=sender,
        media_fetcher=fetcher,
        meta_app_secret=APP_SECRET,
        meta_verify_token=VERIFY_TOKEN,
    )
    client = TestClient(app)

    payload = _payload(_image_message())
    response = _post(client, payload)
    assert response.status_code == 200
    assert fetcher.requested_refs == [MEDIA_ID]
    # The fetch returned nothing, so the agent saw no image and had no text to
    # read — the photo order is escalated, not silently dropped.
    assert len(store.orders) == 1
    assert store.orders[0].status.value == "pending_review"
