"""Production entry point: real Gemini agent + Firestore durable sessions."""

from __future__ import annotations

import uvicorn

from .agent import build_agent, build_session_service
from .config import settings
from .meta_whatsapp import MetaWhatsAppSender
from .store import FirestoreOrderStore, InMemoryOrderStore
from .web import create_app

if settings.session_service == "memory":
    store = InMemoryOrderStore()
else:
    store = FirestoreOrderStore()


def agent_session_factory(firestore_client):
    """Build the agent and session service on the executor's event loop."""
    session_service = build_session_service(firestore_client=firestore_client)
    agent = build_agent(store=store)
    return agent, session_service


app = create_app(
    agent_session_factory=agent_session_factory,
    store=store,
    whatsapp_sender=MetaWhatsAppSender(
        settings.meta_access_token, settings.meta_phone_number_id
    ),
)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.service_port)
