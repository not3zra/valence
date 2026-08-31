"""Production entry point: real Gemini agent + Firestore durable sessions."""

from __future__ import annotations

import uvicorn

from .config import settings


def _make_store():
    """Create the order store lazily so gRPC binds to the running event loop."""
    from .store import FirestoreOrderStore, InMemoryOrderStore

    if settings.session_service == "memory":
        return InMemoryOrderStore()
    return FirestoreOrderStore()


_whatsapp_sender = None


def _get_whatsapp_sender():
    global _whatsapp_sender
    if _whatsapp_sender is None:
        from .meta_whatsapp import MetaWhatsAppSender

        _whatsapp_sender = MetaWhatsAppSender(
            settings.meta_access_token, settings.meta_phone_number_id
        )
    return _whatsapp_sender


def create_valence_app():
    """Build the app with lazy store creation so gRPC binds to the uvicorn loop."""
    from .agent import build_agent, build_session_service
    from .web import create_app

    store = _make_store()

    def agent_session_factory(firestore_client):
        session_service = build_session_service(firestore_client=firestore_client)
        agent = build_agent(store=store)
        return agent, session_service

    sender = _get_whatsapp_sender()
    return create_app(
        agent=build_agent(store=store, whatsapp_sender=sender),
        session_service=build_session_service(),
        store=store,
        whatsapp_sender=sender,
    )


app = create_valence_app()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.service_port)
