"""FastAPI web layer served from the same Cloud Run instance.

Serves the health page, a liveness probe for Cloud Run, the round-trip probe
that exercises the deployed agent (message in -> reply out), and the Twilio
webhooks that receive inbound orders: the WhatsApp "When a message comes in"
callback (ticket 3, #4) and the Voice recording-status callback (ticket 9,
#10). The approver-facing review web view is ticket 5 (#6).
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import time

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from google.adk.agents import Agent
from pydantic import BaseModel, Field

from . import review
from .agent import build_runner, run_turn
from .config import settings
from .core import OrderProcessingCore
from .media import MediaFetcher, TwilioMediaFetcher
from .orders import Order, OrderStatus
from .store import OrderStore
from .twilio import verify_twilio_signature
from .twilio_voice import TwilioVoiceCallbackParser
from .twilio_whatsapp import TwilioWhatsAppParser
from .voice import VoiceCallbackParser
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


# The voice webhook has no customer text to read, only the recording. This
# nudge tells the model the inline audio is a call in which an order was placed
# (issue #10); the agent instruction (src.agent) drives how it is understood.
VOICE_NUDGE = (
    "This audio is a recording of a phone call in which the caller placed an "
    "order. Understand the recording and commit it as a structured order."
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
    <li>Review web view (escalation queue):
    <a href="/review">/review</a></li>
    <li>Agent round-trip probe (message in → reply out):
    <code>POST /api/roundtrip</code></li>
    <li>Twilio Voice recording-status callback (recorded order calls):
    <code>POST /api/voice/callback</code></li>
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
    voice_parser: VoiceCallbackParser | None = None,
    store: OrderStore | None = None,
) -> FastAPI:
    """Build the FastAPI app wired to a specific agent + session service.

    Both are injected so tests can run the whole HTTP surface against a fake
    LLM and an in-memory session service, and so the production entry point
    (`src.main`) wires the real Gemini agent + Firestore session service.

    ``whatsapp_sender`` defaults to ``MockWhatsAppSender`` (the demo path,
    issue #4 design note); ``webhook_parser`` defaults to the Twilio WhatsApp
    adapter; ``voice_parser`` defaults to the Twilio Voice recording-status
    adapter (issue #10); ``media_fetcher`` defaults to ``TwilioMediaFetcher``
    (the photo/recording retrieval path); ``twilio_auth_token`` defaults to the
    configured value and is what both webhooks use to verify the
    ``X-Twilio-Signature`` header.
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
    callback_parser = voice_parser or TwilioVoiceCallbackParser()

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
    def roundtrip(request: Request, payload: RoundTripRequest) -> RoundTripResponse:
        # Debug probe (issue #4): caller supplies the sender id, so when a
        # shared secret is configured it must be presented or the probe is
        # closed — otherwise an unauthenticated caller could drive the agent
        # under an arbitrary identity (including an allowlisted approver, issue
        # #7). The webhook path is the real sender-verified channel.
        expected = f"Bearer {auth_token}" if auth_token else None
        if expected and not hmac.compare_digest(
            request.headers.get("Authorization", ""), expected
        ):
            raise HTTPException(status_code=401)
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

    @app.post("/api/voice/callback")
    async def voice_callback(request: Request) -> Response:
        """Twilio Voice's recording-status callback (ticket 9, #10).

        When a call recording completes, Twilio POSTs the callback carrying the
        recording's URL. This endpoint verifies the ``X-Twilio-Signature``
        header (the same HMAC-SHA1 algorithm the WhatsApp webhook uses), fetches
        the recording from the Twilio URL through the ``MediaFetcher`` seam
        (basic auth, allowlisted host, no redirects, size-capped — the same
        guards behind photo intake, issue #11), and passes it to the same ADK
        agent as inline audio, understood through the same Gemini call as text.
        The agent commits ``source_channel="voice"``, so a voice order with a
        missing field is escalated as flagged — it never waits on a clarifying
        question (ADR-0004). There is no outbound voice channel in this build,
        so the agent's reply is not delivered anywhere.

        Only a completed recording is processed. A non-completed callback is
        acknowledged and ignored. A fetch that fails returns an error status so
        Twilio retries the recording-status callback rather than losing the
        order — there is no message text to fall back to on the voice channel.
        """
        form: dict[str, str] = {
            key: str(value) for key, value in (await request.form()).items()
        }
        signature = request.headers.get("X-Twilio-Signature", "")
        if not verify_twilio_signature(str(request.url), form, signature, auth_token):
            return _twiml(status_code=403)
        recording = callback_parser.parse(form)
        if recording is None:
            return _twiml()
        media = fetcher.fetch(recording.recording_url)
        if media is None:
            return Response(status_code=500)
        run_turn(
            runner,
            sender_id=recording.caller,
            message=VOICE_NUDGE,
            media=media,
        )
        return _twiml()

    if store is not None:
        _register_review_routes(app, store)

    return app


def _passcode_digest(passcode: str) -> str:
    """Deterministic token proving knowledge of the demo passcode.

    The cookie carries this digest, never the passcode itself; the gate
    recomputes it from the configured passcode and compares constant-time.
    """
    return hashlib.sha256(passcode.encode()).hexdigest()


def _register_review_routes(app: FastAPI, store: OrderStore) -> None:
    """Register the passcode-gated review web view (issue #6).

    Every ``/review`` route is gated by a single demo passcode read from the
    store config; a request without a valid session cookie is redirected to the
    login page (or rejected, for JSON/POST endpoints). The web decision path
    calls ``approve_order_web`` on the same Order Processing Core the ADK agent
    uses, so web and WhatsApp approvals stay in sync and share one audit trail.
    """
    core = OrderProcessingCore(store)

    async def _passcode() -> str:
        config = await store.get_config()
        value = config.get("web_passcode")
        if not value:
            raise HTTPException(status_code=503, detail="review view is not configured")
        return str(value)

    async def _authorized(request: Request) -> bool:
        expected = _passcode_digest(await _passcode())
        cookie = request.cookies.get(review.PASSCODE_COOKIE, "")
        return bool(cookie) and hmac.compare_digest(cookie, expected)

    async def _require(request: Request) -> None:
        if not await _authorized(request):
            raise HTTPException(status_code=401)

    async def _orders() -> list[Order]:
        orders = await store.list_all_orders()
        orders.sort(key=lambda o: o.created_at, reverse=True)
        return orders

    async def _searchable_text(order: Order) -> str:
        order_id = order.order_id or ""
        events = await store.list_order_events(order_id)
        fields = " ".join(
            [
                order_id,
                order.phone,
                order.customer or "",
                order.delivery_location or "",
                *(e.event_type for e in events),
                *(str(e.payload) for e in events),
            ]
        )
        return fields.lower()

    async def _stats() -> dict[str, int]:
        config = await store.get_config()
        cutoff = str(config.get("cutoff_time"))
        hour, minute = (int(part) for part in cutoff.split(":")) if cutoff else (
            review.DEFAULT_CUTOFF_TIME.hour,
            review.DEFAULT_CUTOFF_TIME.minute,
        )
        return review.compute_stats(
            await _orders(), cutoff_time=time(hour, minute)
        )

    @app.get("/review", response_class=HTMLResponse)
    async def review_index(request: Request):
        if not await _authorized(request):
            return review.login_page()
        escalated = [
            o for o in await _orders()
            if o.status is OrderStatus.PENDING_REVIEW
        ]
        return review.queue_page(escalated, await _stats())

    @app.get("/review/orders", response_class=HTMLResponse)
    async def review_orders(request: Request, q: str | None = None):
        if not await _authorized(request):
            return review.login_page()
        orders = await _orders()
        if q:
            needle = q.strip().lower()
            matches = []
            for o in orders:
                if needle in await _searchable_text(o):
                    matches.append(o)
            orders = matches
        return review.queue_page(orders, await _stats(), q=q)

    @app.get("/review/orders/{order_id}", response_class=HTMLResponse)
    async def review_order_detail(
        request: Request, order_id: str, message: str | None = None
    ):
        await _require(request)
        order = await store.get_order(order_id)
        if order is None:
            raise HTTPException(status_code=404)
        events = await store.list_order_events(order_id)
        events.sort(key=lambda e: e.created_at)
        return review.order_page(order, events, message=message)

    @app.post("/review/login", response_class=HTMLResponse)
    async def review_login(request: Request):
        form = await request.form()
        submitted = str(form.get("passcode", ""))
        if not hmac.compare_digest(submitted, await _passcode()):
            return review.login_page(error="Incorrect passcode.")
        response = RedirectResponse("/review", status_code=303)
        response.set_cookie(
            review.PASSCODE_COOKIE,
            _passcode_digest(await _passcode()),
            httponly=True,
            samesite="lax",
        )
        return response

    @app.post("/review/logout")
    async def review_logout():
        response = RedirectResponse("/review", status_code=303)
        response.delete_cookie(review.PASSCODE_COOKIE)
        return response

    @app.get("/review/stats")
    async def review_stats(request: Request):
        await _require(request)
        return await _stats()

    async def _decide(
        request: Request, order_id: str, approved: bool
    ) -> RedirectResponse:
        await _require(request)
        action = "approve" if approved else "reject"
        try:
            await core.approve_order_web(order_id, approved=approved)
        except Exception:
            return RedirectResponse(
                f"/review/orders/{order_id}?message=Could not {action} this order.",
                status_code=303,
            )
        return RedirectResponse(f"/review/orders/{order_id}", status_code=303)

    @app.post("/review/orders/{order_id}/approve")
    async def review_approve(request: Request, order_id: str):
        return await _decide(request, order_id, approved=True)

    @app.post("/review/orders/{order_id}/reject")
    async def review_reject(request: Request, order_id: str):
        return await _decide(request, order_id, approved=False)
