"""FastAPI web layer served from the same Cloud Run instance.

Serves the health page, a liveness probe for Cloud Run, the round-trip probe
that exercises the deployed agent (message in -> reply out), and the Twilio
WhatsApp webhook that receives inbound orders (ticket 3, #4). The
approver-facing review web view is ticket 5 (#6).
"""

from __future__ import annotations

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse
from google.adk.agents import Agent
from pydantic import BaseModel, Field

from .agent import build_runner, run_turn
from .config import settings
from .media import MediaFetcher, TwilioMediaFetcher
from .twilio_whatsapp import TwilioWhatsAppParser, verify_twilio_signature
from .whatsapp import MockWhatsAppSender, WhatsAppSender, WhatsAppWebhookParser


def _twiml(status_code: int = 200) -> Response:
    """An empty TwiML <Response/> tells Twilio the webhook was handled; the
    actual confirmation reply is delivered out-of-band through the sender seam.
    """
    return Response(
        content="<Response></Response>",
        media_type="application/xml",
        status_code=status_code,
    )

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
    <li>Health: <a href="/health">/health</a></li>
    <li>Agent round-trip probe (message in → reply out):
    <code>POST /api/roundtrip</code></li>
  </ul>
</body>
</html>
"""


E164_PATTERN = r"^\+[1-9]\d{1,14}$"


class RoundTripRequest(BaseModel):
    sender_id: str = Field(
        pattern=E164_PATTERN,
        min_length=3,
        description="Verified phone number of the sender (E.164)",
    )
    message: str = Field(min_length=1, description="Inbound message text")


class RoundTripResponse(BaseModel):
    sender_id: str
    reply: str


def create_app(
    *,
    agent: Agent,
    session_service,
    whatsapp_sender: WhatsAppSender | None = None,
    webhook_parser: WhatsAppWebhookParser | None = None,
    media_fetcher: MediaFetcher | None = None,
    twilio_auth_token: str | None = None,
) -> FastAPI:
    """Build the FastAPI app wired to a specific agent + session service.

    Both are injected so tests can run the whole HTTP surface against a fake
    LLM and an in-memory session service, and so the production entry point
    (`src.main`) wires the real Gemini agent + Firestore session service.

    ``whatsapp_sender`` defaults to ``MockWhatsAppSender`` (the demo path,
    issue #4 design note); ``webhook_parser`` defaults to the Twilio adapter;
    ``media_fetcher`` defaults to ``TwilioMediaFetcher`` (the photo intake
    path, issue #11); ``twilio_auth_token`` defaults to the configured value
    and is what the webhook uses to verify the ``X-Twilio-Signature`` header.
    """
    runner = build_runner(agent, session_service)
    sender = whatsapp_sender or MockWhatsAppSender()
    parser = webhook_parser or TwilioWhatsAppParser()
    auth_token = (
        twilio_auth_token
        if twilio_auth_token is not None
        else settings.twilio_auth_token
    )
    fetcher = media_fetcher or TwilioMediaFetcher(
        settings.twilio_account_sid, auth_token
    )

    app = FastAPI(title="Valence — Order Intake & Fulfillment")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return INDEX_HTML

    # NOTE: /healthz (and any path ending in "z") is intercepted by Cloud Run's
    # edge — reserved for Google's own health probes — so the liveness probe is
    # served at /health instead.
    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/roundtrip", response_model=RoundTripResponse)
    def roundtrip(payload: RoundTripRequest) -> RoundTripResponse:
        reply = run_turn(runner, sender_id=payload.sender_id, message=payload.message)
        return RoundTripResponse(sender_id=payload.sender_id, reply=reply)

    @app.post("/api/whatsapp/webhook")
    async def whatsapp_webhook(request: Request) -> Response:
        """Twilio's "When a message comes in" webhook.

        Verifies the ``X-Twilio-Signature`` in the adapter, parses the
        form-encoded message through the Twilio adapter, runs one agent turn
        (Gemini extraction -> ``process_order`` commit), and delivers the
        agent's confirmation — including the estimated total from draft
        pricing — back over WhatsApp through the sender seam. Returns an empty
        TwiML response; the reply travels out-of-band.

        A message carrying a photo (issue #11) fetches the first media object
        from the Twilio URL with the required basic auth and passes it to the
        agent as an inline image, understood in the same Gemini call as text.
        A media fetch that fails falls back to handling the text the message
        carried, rather than failing the whole request.
        """
        form: dict[str, str] = {
            key: str(value) for key, value in (await request.form()).items()
        }
        signature = request.headers.get("X-Twilio-Signature", "")
        if not verify_twilio_signature(
            str(request.url), form, signature, auth_token
        ):
            return _twiml(status_code=403)
        message = parser.parse(form)
        if message is None or (not message.body and not message.media):
            return _twiml()
        media = (
            fetcher.fetch(message.media[0]) if message.media else None
        )
        reply = run_turn(
            runner, sender_id=message.sender, message=message.body, media=media
        )
        if reply:
            sender.send(message.sender, reply)
        return _twiml()

    return app
