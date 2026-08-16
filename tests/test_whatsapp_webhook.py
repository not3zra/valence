"""Meta WhatsApp webhook endpoint (issue #4 tracer bullet, swapped to Meta #13).

A Meta webhook POST (nested JSON as Meta sends it) is signature-verified in
the Meta adapter, parsed into a neutral ``InboundMessage``, driven through a
real ADK agent turn whose ``process_order`` tool commits the order, and the
agent's confirmation reply — including the estimated total from draft pricing
— is delivered through the ``WhatsAppSender`` seam. The GET verification
handshake echoes ``hub.challenge`` only for a matching verify token. Invalid
signatures are rejected before anything is committed.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from google.adk.sessions import InMemorySessionService

from src.agent import build_agent
from src.meta_whatsapp import build_meta_signature
from src.store import InMemoryOrderStore
from src.web import create_app
from src.whatsapp import MockWhatsAppSender

from .fakes import ClarifyingToolCallingLlm, ConfirmingToolCallingLlm

WEBHOOK_URL = "/api/whatsapp/webhook"
APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"


def _payload(messages: list[dict], **overrides) -> dict:
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
                            "messages": messages,
                            **overrides,
                        },
                    }
                ],
            }
        ],
    }


def _text_message(body: str, sender: str = "919812345001") -> dict:
    return {
        "from": sender,
        "id": "wamid.test",
        "timestamp": "1700000000",
        "type": "text",
        "text": {"body": body},
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


@pytest.fixture
def webhook():
    sender = MockWhatsAppSender()
    store = InMemoryOrderStore()
    app = create_app(
        agent=build_agent(model=ConfirmingToolCallingLlm(), store=store),
        session_service=InMemorySessionService(),
        whatsapp_sender=sender,
        meta_app_secret=APP_SECRET,
        meta_verify_token=VERIFY_TOKEN,
    )
    return TestClient(app), sender, store


def test_get_handshake_echoes_challenge_for_matching_token(webhook):
    client, _, _ = webhook
    response = client.get(
        WEBHOOK_URL,
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "challenge-abc",
        },
    )
    assert response.status_code == 200
    assert response.text == "challenge-abc"


def test_get_handshake_rejects_wrong_verify_token(webhook):
    client, _, _ = webhook
    response = client.get(
        WEBHOOK_URL,
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong",
            "hub.challenge": "challenge-abc",
        },
    )
    assert response.status_code == 403


def test_get_handshake_fails_closed_when_no_token_configured():
    sender = MockWhatsAppSender()
    store = InMemoryOrderStore()
    app = create_app(
        agent=build_agent(model=ConfirmingToolCallingLlm(), store=store),
        session_service=InMemorySessionService(),
        whatsapp_sender=sender,
        meta_app_secret=APP_SECRET,
        meta_verify_token=None,
    )
    client = TestClient(app)
    response = client.get(
        WEBHOOK_URL,
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "anything",
            "hub.challenge": "challenge-abc",
        },
    )
    assert response.status_code == 403


def test_get_without_handshake_params_is_rejected(webhook):
    # A bare GET is not a Meta verification handshake and carries no signature
    # to verify — it is refused, never served anything.
    client, _, _ = webhook
    response = client.get(WEBHOOK_URL)
    assert response.status_code == 403


def test_webhook_commits_order_and_sends_confirmation_with_total(webhook):
    client, sender, store = webhook
    payload = _payload([_text_message("Namaste, 2 drums sulfuric acid chahiye")])
    response = _post(client, payload)
    assert response.status_code == 200

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


def test_webhook_processes_every_message_in_a_delivered_batch(webhook):
    client, sender, store = webhook
    payload = _payload(
        [
            _text_message("Namaste, 2 drums sulfuric acid chahiye", "919812345001"),
            _text_message("Namaste, 2 drums sulfuric acid chahiye", "919812345002"),
        ]
    )
    response = _post(client, payload)
    assert response.status_code == 200

    assert len(store.orders) == 2
    assert {order.phone for order in store.orders} == {
        "+919812345001",
        "+919812345002",
    }
    # Each sender is a different seeded customer (ChemFab vs Maruthi Coatings,
    # different agreed rates), so the confirmations carry each order's own
    # estimated total; what matters is that every message in the batch was
    # processed and answered, none silently dropped.
    assert len(sender.sent) == 2
    assert all("Order confirmed" in text for _, text in sender.sent)
    assert {recipient for recipient, _ in sender.sent} == {
        "+919812345001",
        "+919812345002",
    }


def test_webhook_escalated_order_stays_escalated(webhook):
    client, sender, store = webhook
    payload = _payload(
        [_text_message("500 drums of sulfuric acid please", "919999999999")]
    )
    response = _post(client, payload)
    assert response.status_code == 200
    assert store.orders[-1].status.value == "pending_review"
    assert "unknown_customer" in store.orders[-1].escalation_reasons
    assert "under approval" in sender.sent[0][1]


def test_webhook_rejects_invalid_signature(webhook):
    client, sender, store = webhook
    payload = _payload([_text_message("Namaste")])
    response = _post(client, payload, headers={"X-Hub-Signature-256": "sha256=bogus"})
    assert response.status_code == 403
    assert store.orders == []
    assert sender.sent == []


def test_webhook_rejects_missing_signature(webhook):
    client, sender, store = webhook
    payload = _payload([_text_message("Namaste")])
    response = _post(client, payload, headers={})
    assert response.status_code == 403
    assert store.orders == []
    assert sender.sent == []


def test_webhook_rejects_tampered_body(webhook):
    # The signature is computed over the exact raw body Meta sent; a request
    # carrying the same logical payload re-encoded with different bytes (here:
    # different JSON spacing) must fail verification even though it parses to
    # the same dict.
    client, sender, store = webhook
    payload = _payload([_text_message("Namaste")])
    body = json.dumps(payload).encode()
    response = client.post(
        WEBHOOK_URL,
        content=body,
        headers={
            "content-type": "application/json",
            "X-Hub-Signature-256": build_meta_signature(body, APP_SECRET),
        },
    )
    assert response.status_code == 200
    assert len(store.orders) == 1

    reencoded = json.dumps(payload, separators=(",", ":")).encode()
    assert reencoded != body
    response = client.post(
        WEBHOOK_URL,
        content=reencoded,
        headers={
            "content-type": "application/json",
            "X-Hub-Signature-256": build_meta_signature(body, APP_SECRET),
        },
    )
    assert response.status_code == 403
    assert len(store.orders) == 1


def test_webhook_rejects_oversized_body(webhook):
    # The endpoint is signature-gated, not network-gated, so an oversized body
    # must be refused with 413 before it is buffered (CWE-400), matching the
    # 64 KiB cap on the cutoff envelope.
    client, sender, store = webhook
    oversized = "x" * (64 * 1024 + 1)
    response = client.post(
        WEBHOOK_URL,
        content=oversized,
        headers={
            "content-type": "text/plain",
            "X-Hub-Signature-256": "sha256=anything",
        },
    )
    assert response.status_code == 413
    assert store.orders == []
    assert sender.sent == []


def test_webhook_acknowledges_echo_status_callback(webhook):
    # A Meta callback with no messages (e.g. a status update or an echo) is
    # acknowledged and ignored — nothing is committed, nothing is sent.
    client, sender, store = webhook
    payload = _payload(messages=[], contacts=[])
    response = _post(client, payload)
    assert response.status_code == 200
    assert store.orders == []
    assert sender.sent == []


def test_webhook_repeat_within_window_replies_already_received(webhook):
    client, sender, store = webhook
    payload = _payload([_text_message("2 drums sulfuric acid chahiye")])
    for _ in range(2):
        response = _post(client, payload)
        assert response.status_code == 200

    assert len(store.orders) == 1
    assert store.orders[0].status.value == "approved"
    assert sender.sent[0][1].startswith("Order confirmed.")
    assert "already been received" in sender.sent[-1][1]
    assert sender.sent[-1][1] not in sender.sent[0][1]


def test_webhook_missing_field_asks_then_completes_on_reply():
    sender = MockWhatsAppSender()
    store = InMemoryOrderStore()
    app = create_app(
        agent=build_agent(model=ClarifyingToolCallingLlm(), store=store),
        session_service=InMemorySessionService(),
        whatsapp_sender=sender,
        meta_app_secret=APP_SECRET,
        meta_verify_token=VERIFY_TOKEN,
    )
    client = TestClient(app)

    # First message: missing delivery location -> a clarifying question is sent,
    # nothing is persisted and nothing escalates.
    payload = _payload([_text_message("2 drums sulfuric acid chahiye")])
    response = _post(client, payload)
    assert response.status_code == 200
    assert store.orders == []
    assert store.events == []
    assert sender.sent[0][1] == "Where should we deliver? Please share the location."

    # Customer replies with the location; the same session resumes and the
    # completed order commits.
    reply = _payload([_text_message("Peenya Industrial Area")])
    response = _post(client, reply)
    assert response.status_code == 200

    assert len(store.orders) == 1
    order = store.orders[0]
    assert order.status.value == "approved"
    assert order.delivery_location_id == "dl_peenya"
    assert "Order confirmed" in sender.sent[-1][1]
