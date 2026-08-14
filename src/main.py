"""Production entry point: real Gemini agent + Firestore durable sessions."""

from __future__ import annotations

import uvicorn

from .agent import build_agent, build_session_service
from .config import settings
from .store import FirestoreOrderStore
from .web import create_app

store = FirestoreOrderStore()

app = create_app(
    agent=build_agent(store=store),
    session_service=build_session_service(),
    store=store,
)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.service_port)
