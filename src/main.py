"""Production entry point: real Gemini agent + Firestore durable sessions."""

from __future__ import annotations

import uvicorn

from .agent import build_agent, build_session_service
from .config import settings
from .meta_whatsapp import MetaWhatsAppSender
from .store import FirestoreOrderStore
from .web import create_app

store = FirestoreOrderStore()

app = create_app(
    agent=build_agent(store=store),
    session_service=build_session_service(),
    store=store,
    # Deployment wiring selects Meta as the live WhatsApp sender (issue #13);
    # MockWhatsAppSender remains the test/demo default. The sender fails
    # closed when META_ACCESS_TOKEN / META_PHONE_NUMBER_ID are unset.
    whatsapp_sender=MetaWhatsAppSender(
        settings.meta_access_token, settings.meta_phone_number_id
    ),
)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.service_port)
