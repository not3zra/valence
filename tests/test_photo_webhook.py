"""Photo intake over the WhatsApp webhook (issue #11).

A webhook message carrying a media URL fetches the photo through the
``MediaFetcher`` seam (with the required basic auth), passes it to the ADK
agent as an inline image, and the agent reads it into the same structured
order shape as text — flowing through the Order Processing Core exactly like a
text order. A photo whose handwriting cannot be read falls back to the
escalation path rather than a guessed order.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from google.adk.sessions import InMemorySessionService

from src.agent import build_agent
from src.media import MediaObject
from src.store import InMemoryOrderStore
from src.twilio_whatsapp import build_twilio_signature
from src.web import create_app
from src.whatsapp import MockWhatsAppSender

from .fakes import PhotoReadingLlm, UnreadablePhotoLlm

WEBHOOK_URL = "/api/whatsapp/webhook"
AUTH_TOKEN = "test-auth-token"


class FakeMediaFetcher:
    def __init__(self, media: MediaObject | None = None) -> None:
        self.media = media
        self.requested_urls: list[str] = []

    def fetch(self, url: str) -> MediaObject | None:
        self.requested_urls.append(url)
        return self.media


def _sign(form: dict[str, str]) -> str:
    return build_twilio_signature(
        f"http://testserver{WEBHOOK_URL}", form, AUTH_TOKEN
    )


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
        twilio_auth_token=AUTH_TOKEN,
    )
    client = TestClient(app)

    form = {
        "From": "whatsapp:+919812345001",
        "Body": "",
        "NumMedia": "1",
        "MediaUrl0": "https://api.twilio.com/.../Media/MEphoto",
    }
    response = client.post(
        WEBHOOK_URL,
        data=form,
        headers={"X-Twilio-Signature": _sign(form)},
    )
    assert response.status_code == 200
    assert "<Response" in response.text

    assert fetcher.requested_urls == ["https://api.twilio.com/.../Media/MEphoto"]
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
        twilio_auth_token=AUTH_TOKEN,
    )
    client = TestClient(app)

    form = {
        "From": "whatsapp:+919812345001",
        "Body": "",
        "NumMedia": "1",
        "MediaUrl0": "https://api.twilio.com/.../Media/MEunreadable",
    }
    response = client.post(
        WEBHOOK_URL,
        data=form,
        headers={"X-Twilio-Signature": _sign(form)},
    )
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
        twilio_auth_token=AUTH_TOKEN,
    )
    client = TestClient(app)

    form = {
        "From": "whatsapp:+919812345001",
        "Body": "",
        "NumMedia": "1",
        "MediaUrl0": "https://api.twilio.com/.../Media/MEgone",
    }
    response = client.post(
        WEBHOOK_URL,
        data=form,
        headers={"X-Twilio-Signature": _sign(form)},
    )
    assert response.status_code == 200
    assert fetcher.requested_urls == ["https://api.twilio.com/.../Media/MEgone"]
    # The fetch returned nothing, so the agent saw no image and had no text to
    # read — the photo order is escalated, not silently dropped.
    assert len(store.orders) == 1
    assert store.orders[0].status.value == "pending_review"
