"""Production entry point: real Gemini agent + Firestore durable sessions."""

from __future__ import annotations

import uvicorn

from .agent import build_agent, build_session_service
from .config import settings
from .meta_whatsapp import MetaWhatsAppSender
from .store import FirestoreOrderStore
from .web import create_app

store = FirestoreOrderStore()

_whatsapp_sender = MetaWhatsAppSender(
    settings.meta_access_token, settings.meta_phone_number_id
)

app = create_app(
    agent=build_agent(store=store, whatsapp_sender=_whatsapp_sender),
    session_service=build_session_service(),
    store=store,
    whatsapp_sender=_whatsapp_sender,
)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.service_port)
