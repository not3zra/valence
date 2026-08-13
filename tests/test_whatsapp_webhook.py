"""Twilio WhatsApp webhook endpoint (issue #4 tracer bullet).

A form-encoded webhook POST (as Twilio sends it) is signature-verified in the
adapter, parsed into a neutral ``InboundMessage``, driven through a real ADK
agent turn whose ``process_order`` tool commits the order, and the agent's
confirmation reply — including the estimated total from draft pricing — is
delivered through the ``WhatsAppSender`` seam. An escalated order stays
escalated. Invalid signatures are rejected before anything is committed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from google.adk.sessions import InMemorySessionService

from src.agent import build_agent
from src.store import InMemoryOrderStore
from src.twilio_whatsapp import build_twilio_signature
from src.web import create_app
from src.whatsapp import MockWhatsAppSender

from .fakes import ConfirmingToolCallingLlm

WEBHOOK_URL = "/api/whatsapp/webhook"
AUTH_TOKEN = "test-auth-token"


@pytest.fixture
def webhook():
    sender = MockWhatsAppSender()
    store = InMemoryOrderStore()
    app = create_app(
        agent=build_agent(model=ConfirmingToolCallingLlm(), store=store),
        session_service=InMemorySessionService(),
        whatsapp_sender=sender,
        twilio_auth_token=AUTH_TOKEN,
    )
    return TestClient(app), sender, store


def _sign(form: dict[str, str]) -> str:
    # The signature is computed over the exact URL the request reaches the
    # service at (TestClient's base URL) plus the form fields, sorted.
    return build_twilio_signature(
        f"http://testserver{WEBHOOK_URL}", form, AUTH_TOKEN
    )


def test_webhook_commits_order_and_sends_confirmation_with_total(webhook):
    client, sender, store = webhook
    form = {
        "From": "whatsapp:+919812345001",
        "Body": "Namaste, 2 drums sulfuric acid chahiye",
        "NumMedia": "0",
    }
    response = client.post(
        WEBHOOK_URL,
        data=form,
        headers={"X-Twilio-Signature": _sign(form)},
    )
    assert response.status_code == 200
    assert "<Response" in response.text

    assert len(store.orders) == 1
    order = store.orders[0]
    assert order.phone == "+919812345001"
    assert order.status.value == "approved"

    assert sender.sent == [
        (
            "+919812345001",
            "Order confirmed. Estimated total: 35,000 INR.",
        )
    ]
    assert "35,000" in sender.sent[0][1]


def test_webhook_escalated_order_stays_escalated(webhook):
    client, sender, store = webhook
    form = {
        "From": "whatsapp:+919999999999",
        "Body": "500 drums of sulfuric acid please",
        "NumMedia": "0",
    }
    response = client.post(
        WEBHOOK_URL,
        data=form,
        headers={"X-Twilio-Signature": _sign(form)},
    )
    assert response.status_code == 200
    assert store.orders[-1].status.value == "pending_review"
    assert "unknown_customer" in store.orders[-1].escalation_reasons
    assert "under approval" in sender.sent[0][1]


def test_webhook_rejects_invalid_signature(webhook):
    client, sender, store = webhook
    form = {"From": "whatsapp:+919812345001", "Body": "Namaste", "NumMedia": "0"}
    response = client.post(
        WEBHOOK_URL,
        data=form,
        headers={"X-Twilio-Signature": "bogus-signature"},
    )
    assert response.status_code == 403
    assert store.orders == []
    assert sender.sent == []


def test_webhook_rejects_missing_signature(webhook):
    client, sender, store = webhook
    form = {"From": "whatsapp:+919812345001", "Body": "Namaste", "NumMedia": "0"}
    response = client.post(WEBHOOK_URL, data=form)
    assert response.status_code == 403
    assert store.orders == []
    assert sender.sent == []
