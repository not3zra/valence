"""Smoke test: drive the Twilio WhatsApp webhook path with a real agent.

Simulates Twilio's inbound webhook (form-encoded ``From``/``Body`` with a valid
``X-Twilio-Signature``) against the running service, exercising the ticket 3
acceptance: a message arrives via the webhook, the agent commits the order
through the Order Processing Core, and the confirmation reply — including the
estimated total from draft pricing — is produced. The reply is captured by the
MockWhatsAppSender (the demo outbound seam), not actually delivered.

Requires ``GOOGLE_API_KEY`` and ``TWILIO_AUTH_TOKEN``. Run the service first
via ``./scripts/run_local.sh`` (emulator) or point at the deployed instance.

Usage:
    GOOGLE_API_KEY=... TWILIO_AUTH_TOKEN=... python scripts/smoke_whatsapp_webhook.py \\
        --from +919812345001 --message "Namaste, 2 drums sulfuric acid chahiye"
"""

from __future__ import annotations

import argparse
import os

from fastapi.testclient import TestClient

from src.agent import build_agent, build_session_service
from src.store import FirestoreOrderStore
from src.twilio_whatsapp import build_twilio_signature
from src.web import create_app
from src.whatsapp import MockWhatsAppSender

WEBHOOK_URL = "/api/whatsapp/webhook"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="sender", default="+919812345001")
    parser.add_argument(
        "--message",
        default="Namaste, 2 drums sulfuric acid chahiye",
    )
    args = parser.parse_args()

    token = os.environ["TWILIO_AUTH_TOKEN"]
    sender = MockWhatsAppSender()
    app = create_app(
        agent=build_agent(store=FirestoreOrderStore()),
        session_service=build_session_service(),
        whatsapp_sender=sender,
        twilio_auth_token=token,
    )
    form = {
        "From": f"whatsapp:{args.sender}",
        "Body": args.message,
        "NumMedia": "0",
    }
    signature = build_twilio_signature(
        f"http://testserver{WEBHOOK_URL}", form, token
    )

    client = TestClient(app)
    response = client.post(
        WEBHOOK_URL,
        data=form,
        headers={"X-Twilio-Signature": signature},
    )
    print(f"webhook status: {response.status_code}")
    for recipient, text in sender.sent:
        print(f"sent -> {recipient}: {text}")


if __name__ == "__main__":
    main()
