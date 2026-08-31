"""Production entry point: real Gemini agent + Firestore durable sessions."""

from __future__ import annotations

import uvicorn

from .config import settings

store = None


def _ensure_store():
    global store
    if store is None:
        from .store import FirestoreOrderStore, InMemoryOrderStore

        if settings.session_service == "memory":
            store = InMemoryOrderStore()
        else:
            store = FirestoreOrderStore()


def create_valence_app():
    from .agent import build_agent, build_session_service
    from .meta_whatsapp import MetaWhatsAppSender
    from .web import create_app

    _ensure_store()

    def agent_session_factory(firestore_client):
        session_service = build_session_service(firestore_client=firestore_client)
        agent = build_agent(store=store)
        return agent, session_service

    sender = MetaWhatsAppSender(
        settings.meta_access_token, settings.meta_phone_number_id
    )

    return create_app(
        agent=build_agent(store=store, whatsapp_sender=sender),
        session_service=build_session_service(),
        store=store,
        whatsapp_sender=sender,
    )


app = create_valence_app()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.service_port)
