"""Smoke test: drive the Meta Cloud API WhatsApp webhook path with a real agent.

Simulates Meta's inbound webhook (nested JSON with a valid
``X-Hub-Signature-256``) against the running service, exercising the ticket 13
acceptance: a message arrives via the webhook, the agent commits the order
through the Order Processing Core, and the confirmation reply — including the
estimated total from draft pricing — is produced. The reply is captured by the
MockWhatsAppSender (the demo outbound seam), not actually delivered.

Requires ``GOOGLE_API_KEY`` and ``META_APP_SECRET``. Run the service first via
``./scripts/run_local.sh`` (emulator) or point at the deployed instance.

Usage:
    GOOGLE_API_KEY=... META_APP_SECRET=... python scripts/smoke_whatsapp_webhook.py \\
        --from +919812345001 --message "Namaste, 2 drums sulfuric acid chahiye"
"""

from __future__ import annotations

import argparse
import json
import os

from fastapi.testclient import TestClient

from src.agent import build_agent, build_session_service
from src.meta_whatsapp import build_meta_signature
from src.store import FirestoreOrderStore
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

    app_secret = os.environ["META_APP_SECRET"]
    sender = MockWhatsAppSender()
    app = create_app(
        agent=build_agent(store=FirestoreOrderStore()),
        session_service=build_session_service(),
        whatsapp_sender=sender,
        meta_app_secret=app_secret,
        meta_verify_token="unused-in-smoke",
    )
    payload = {
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
                                "phone_number_id": "1",
                            },
                            "messages": [
                                {
                                    "from": args.sender.lstrip("+"),
                                    "id": "wamid.smoke",
                                    "timestamp": "1700000000",
                                    "type": "text",
                                    "text": {"body": args.message},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode()
    signature = build_meta_signature(body, app_secret)

    client = TestClient(app)
    response = client.post(
        WEBHOOK_URL,
        content=body,
        headers={
            "content-type": "application/json",
            "X-Hub-Signature-256": signature,
        },
    )
    print(f"webhook status: {response.status_code}")
    for recipient, text in sender.sent:
        print(f"sent -> {recipient}: {text}")


if __name__ == "__main__":
    main()
