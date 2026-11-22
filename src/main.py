"""Production entry point: real Gemini agent, configuration-selected adapters.

The default configuration runs without Google Cloud (in-memory sessions, orders,
and vouchers). ``SESSION_SERVICE=firestore`` selects the optional Firestore
adapters from the ``[gcp]`` extra; only then is a Google Cloud client built.
"""

from __future__ import annotations

import uvicorn

from .config import settings

store = None


def _uses_firestore() -> bool:
    return settings.session_service == "firestore"


def _ensure_store():
    global store
    if store is None:
        from .store import FirestoreOrderStore, InMemoryOrderStore

        store = FirestoreOrderStore() if _uses_firestore() else InMemoryOrderStore()


def create_valence_app():
    from .agent import build_agent, build_session_service
    from .gcp import create_firestore_client
    from .meta_whatsapp import MetaWhatsAppSender
    from .web import create_app

    _ensure_store()

    def agent_session_factory(firestore_client):
        session_service = build_session_service(client=firestore_client)
        agent = build_agent(store=store)
        return agent, session_service

    sender = MetaWhatsAppSender(
        settings.meta_access_token, settings.meta_phone_number_id
    )

    return create_app(
        agent=build_agent(store=store, whatsapp_sender=sender),
        agent_session_factory=agent_session_factory,
        firestore_client_factory=(
            create_firestore_client if _uses_firestore() else None
        ),
        store=store,
        whatsapp_sender=sender,
    )


app = create_valence_app()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.service_port)
