"""FastAPI web layer served from the same Cloud Run instance.

Serves the health page, a liveness probe for Cloud Run, and the round-trip
probe that exercises the deployed agent (message in -> reply out). The
approver-facing review web view is ticket 5 (#6); the Twilio WhatsApp webhook
arrives with ticket 3 (#4).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from google.adk.agents import Agent
from pydantic import BaseModel, Field

from .agent import build_runner, run_turn

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Valence — Order Intake &amp; Fulfillment</title>
</head>
<body>
  <h1>Valence</h1>
  <p>Order intake &amp; fulfillment agent. One ADK agent receives WhatsApp text,
  phone calls and photos of handwritten orders in any language, then runs a
  graduated human-checked approval loop.</p>
  <ul>
    <li>Health: <a href="/healthz">/healthz</a></li>
    <li>Agent round-trip probe (message in → reply out):
    <code>POST /api/roundtrip</code></li>
  </ul>
</body>
</html>
"""


class RoundTripRequest(BaseModel):
    sender_id: str = Field(description="Verified phone number of the sender")
    message: str = Field(description="Inbound message text")


class RoundTripResponse(BaseModel):
    sender_id: str
    reply: str


def create_app(*, agent: Agent, session_service) -> FastAPI:
    """Build the FastAPI app wired to a specific agent + session service.

    Both are injected so tests can run the whole HTTP surface against a fake
    LLM and an in-memory session service, and so the production entry point
    (`src.main`) wires the real Gemini agent + Firestore session service.
    """
    runner = build_runner(agent, session_service)

    app = FastAPI(title="Valence — Order Intake & Fulfillment")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return INDEX_HTML

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/roundtrip", response_model=RoundTripResponse)
    def roundtrip(payload: RoundTripRequest) -> RoundTripResponse:
        reply = run_turn(runner, sender_id=payload.sender_id, message=payload.message)
        return RoundTripResponse(sender_id=payload.sender_id, reply=reply)

    return app
