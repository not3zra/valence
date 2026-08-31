"""FastAPI web layer served from the same Cloud Run instance.

Serves the health page, a liveness probe for Cloud Run, the round-trip probe
that exercises the deployed agent (message in -> reply out), the webhooks
that receive inbound orders (the Meta Cloud API WhatsApp webhook, ticket 3,
#4, swapped to Meta in #13), and the token-gated company-recorded call
ingestion endpoint (issue #35). The approver-facing review web view is ticket
5 (#6) and the dispatch-facing Loading List web view is ticket 8 (#9).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from datetime import date, time
from urllib.parse import quote, urlparse

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from google.adk.agents import Agent
from pydantic import BaseModel, Field

from . import review
from .agent_exec import TurnExecutor
from .config import settings
from .core import ApprovalError, ConfigurationError, OrderProcessingCore
from .dispatch import LateOrderNotifier
from .loading import (
    PASSCODE_COOKIE as LOADING_PASSCODE_COOKIE,
)
from .loading import (
    load_loading_list,
    loading_login_page,
    render_loading_list_html,
)
from .media import (
    AUDIO_MIME_TYPES,
    MAX_MEDIA_BYTES,
    MediaFetcher,
    MediaObject,
    MetaMediaFetcher,
)
from .meta_whatsapp import MetaWhatsAppParser
from .orders import Order, OrderStatus
from .ratelimit import SlidingWindowRateLimiter
from .store import OrderStore
from .voucher import (
    VoucherError,
    VoucherStore,
    default_voucher_storage,
    prepare_voucher,
)
from .whatsapp import MockWhatsAppSender, WhatsAppSender, WhatsAppWebhookParser

# The WhatsApp webhook is unauthenticated at the network layer (signature
# verification gates it), so the body is capped before it is read — a real Meta
# delivery is a few kilobytes; anything larger is not one. Mirrors the 64 KiB
# cap on the Pub/Sub-adjacent /api/cutoff envelope.
MAX_WEBHOOK_BODY_BYTES = 64 * 1024

# The voice-ingest endpoint (issue #35) is token-gated at the network layer, so
# the cap only has to bound a well-intentioned company recording: base64 of the
# 5 MiB audio cap (MAX_MEDIA_BYTES) plus the JSON envelope.
MAX_VOICE_INGEST_BODY_BYTES = 8 * 1024 * 1024


def _meta_ack() -> Response:
    """A plain 200 acknowledgment for Meta's webhook.

    Meta expects any 200; the actual confirmation reply is delivered
    out-of-band through the sender seam. A rejected request is a plain 403.
    """
    return Response(content="OK", media_type="text/plain")


# The voice intake path has no customer text to read, only the recording. This
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
  <style>
    :root {
      --color-bg: #f8fafc; --color-surface: #ffffff; --color-border: #e2e8f0;
      --color-text: #0f172a; --color-text-secondary: #64748b;
      --color-accent: #0d9488; --color-accent-light: #ccfbf1;
      --font-sans: system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif;
      --radius-lg: 12px; --radius-md: 8px;
      --shadow-sm: 0 1px 2px rgba(0,0,0,0.05);
      --shadow-md: 0 1px 3px rgba(0,0,0,0.07), 0 1px 2px rgba(0,0,0,0.04);
      --transition-fast: 120ms ease-out;
    }
    *, *::before, *::after { box-sizing: border-box; }
    body { font-family: var(--font-sans); margin: 0; background: var(--color-bg);
           color: var(--color-text); -webkit-font-smoothing: antialiased; }
    .hero { max-width: 640px; margin: 0 auto; padding: 80px 24px 60px; text-align: center; }
    .hero h1 { font-size: 2.5rem; font-weight: 800; letter-spacing: -0.03em;
               margin: 0 0 12px; color: var(--color-text); }
    .hero h1 span { color: var(--color-accent); }
    .hero p { font-size: 1.05rem; color: var(--color-text-secondary);
              line-height: 1.7; margin: 0 0 40px; max-width: 480px; margin-left: auto; margin-right: auto; }
    .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
             gap: 16px; max-width: 640px; margin: 0 auto; }
    .card { background: var(--color-surface); border: 1px solid var(--color-border);
            border-radius: var(--radius-lg); padding: 24px; text-decoration: none;
            color: var(--color-text); transition: all var(--transition-fast);
            box-shadow: var(--shadow-sm); display: block; }
    .card:hover { box-shadow: var(--shadow-md); border-color: var(--color-accent);
                  text-decoration: none; transform: translateY(-1px); }
    .card h3 { font-size: 1rem; font-weight: 600; margin: 0 0 6px; }
    .card p { font-size: 0.8125rem; color: var(--color-text-secondary);
              margin: 0; line-height: 1.5; }
    .card .tag { display: inline-block; font-size: 0.6875rem; font-weight: 600;
                 text-transform: uppercase; letter-spacing: 0.05em;
                 color: var(--color-accent); margin-bottom: 8px; }
    .footer { text-align: center; padding: 40px 24px; color: var(--color-text-secondary);
              font-size: 0.8125rem; }
    .footer code { font-family: ui-monospace, SFMono-Regular, monospace;
                   background: #f1f5f9; padding: 2px 6px; border-radius: 4px;
                   font-size: 0.75rem; }
  </style>
</head>
<body>
  <div class="hero">
    <h1><span>Valence</span></h1>
    <p>Order intake &amp; fulfillment agent. Receives WhatsApp text, phone calls
    and photos of handwritten orders in any language, then runs a graduated
    human-checked approval loop.</p>
    <div class="cards">
      <a class="card" href="/review">
        <div class="tag">Operations</div>
        <h3>Review Queue</h3>
        <p>Escalation queue, order detail, approve/reject, edit, and Tally voucher generation.</p>
      </a>
      <a class="card" href="/loading">
        <div class="tag">Dispatch</div>
        <h3>Loading List</h3>
        <p>Printable delivery-day dispatch list with route grouping and late add-ons.</p>
      </a>
      <a class="card" href="/health">
        <div class="tag">System</div>
        <h3>Health Check</h3>
        <p>Liveness probe for Cloud Run. Returns <code>{"{"}"status": "ok"{"}"}</code>.</p>
      </a>
    </div>
  </div>
  <div class="footer">
    <p>API: <code>POST /api/roundtrip</code> &middot; <code>POST /api/voice/ingest</code></p>
  </div>
</body>
</html>
"""


E164_PATTERN = r"^\+[1-9]\d{1,14}$"


def _valid_e164(value: str) -> bool:
    """True only for a whole-string E.164 number (anchored, not a substring).

    ``re.fullmatch`` is stricter than Pydantic's ``pattern`` (``re.search``): a
    trailing space or embedded phone number in a longer string is rejected, so
    the ingest caller identity cannot be smuggled past validation.
    """
    return re.fullmatch(E164_PATTERN, value) is not None


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
    store: OrderStore | None = None,
    roundtrip_token: str | None = None,
    voice_ingest_token: str | None = None,
    web_passcode: str | None = None,
    web_passcode_salt: str | None = None,
    web_cookie_secure: bool | None = None,
    voucher_storage: VoucherStore | None = None,
    meta_app_secret: str | None = None,
    meta_verify_token: str | None = None,
    meta_access_token: str | None = None,
    webhook_rate_limit: int | None = None,
) -> FastAPI:
    """Build the FastAPI app wired to a specific agent + session service.

    Both are injected so tests can run the whole HTTP surface against a fake
    LLM and an in-memory session service, and so the production entry point
    (`src.main`) wires the real Gemini agent + Firestore session service.

    ``whatsapp_sender`` defaults to ``MockWhatsAppSender`` (the test/demo path,
    issue #4 design note); the live sender — ``MetaWhatsAppSender`` — is wired
    by the deployment entry point (`src.main`, issue #13). ``webhook_parser``
    defaults to the Meta Cloud API adapter (``MetaWhatsAppParser``, the live
    inbound channel). ``media_fetcher`` defaults to ``MetaMediaFetcher`` (the
    photo path resolves Meta media ids, issue #13). The Meta secrets
    (``meta_app_secret``, ``meta_verify_token``, ``meta_access_token``) default
    to their environment settings and fail closed when unset (no handshake or
    signature verifies).

    ``roundtrip_token`` gates ``/api/roundtrip``: it must be set (defaulting to
    ``ROUNDTRIP_TOKEN``) and presented as a Bearer token, or the probe is
    closed — an unauthenticated probe could drive the agent under an arbitrary
    identity including an allowlisted approver (issue #7, security #28).
    ``voice_ingest_token`` gates the company-recorded call ingestion endpoint
    ``/api/voice/ingest`` (issue #35): it must be set (defaulting to
    ``VOICE_INGEST_TOKEN``) and presented as a Bearer token, or the endpoint is
    closed. Because the caller is taken from the token-authenticated payload,
    it is trusted company metadata, not caller-ID — the bearer is the only way
    in.
    ``web_passcode`` / ``web_passcode_salt`` gate the review web view; the
    salt makes the session cookie unforgeable without the per-deploy secret
    (security #27). Both default to their environment settings.

    ``store``, when supplied, registers the passcode-gated review web view
    (``/review``, issue #6), the dispatch-facing Loading List web view
    (``/loading``, issue #9) behind the same passcode, and the secret-gated
    Cutoff render endpoint (``/api/cutoff``). The Loading List web view and the
    Cutoff endpoint run off the same ``load_loading_list`` render the ADK tool
    uses. ``voucher_storage`` backs the review view's prepare/download/mark-
    billed voucher actions (issue #8); it defaults to the configured Cloud
    Storage bucket, or the in-memory double when none is set.

    ``webhook_rate_limit`` caps agent turns per WhatsApp sender per minute on
    the webhook path (security #32); it defaults to ``WEBHOOK_RATE_LIMIT_PER_SENDER``
    and is enforced by an in-memory per-instance limiter.
    """
    runner = TurnExecutor(agent, session_service)
    sender = whatsapp_sender or MockWhatsAppSender()
    meta_app_secret_value = (
        meta_app_secret
        if meta_app_secret is not None
        else settings.meta_app_secret
    )
    meta_verify_token_value = (
        meta_verify_token
        if meta_verify_token is not None
        else settings.meta_verify_token
    )
    meta_access_token_value = (
        meta_access_token
        if meta_access_token is not None
        else settings.meta_access_token
    )
    parser = webhook_parser or MetaWhatsAppParser(
        meta_app_secret_value, meta_verify_token_value
    )
    fetcher = media_fetcher or MetaMediaFetcher(meta_access_token_value)
    probe_token = (
        roundtrip_token
        if roundtrip_token is not None
        else settings.roundtrip_token
    )
    ingest_token = (
        voice_ingest_token
        if voice_ingest_token is not None
        else settings.voice_ingest_token
    )
    passcode = (
        web_passcode if web_passcode is not None else settings.web_passcode
    )
    passcode_salt = (
        web_passcode_salt
        if web_passcode_salt is not None
        else settings.web_passcode_salt
    )
    cookie_secure = (
        web_cookie_secure
        if web_cookie_secure is not None
        else settings.web_cookie_secure
    )
    rate_limit = (
        webhook_rate_limit
        if webhook_rate_limit is not None
        else settings.webhook_rate_limit
    )
    rate_limiter = SlidingWindowRateLimiter(
        window_seconds=60.0, max_events=rate_limit
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
    def roundtrip(request: Request, payload: RoundTripRequest) -> RoundTripResponse:
        # Debug probe (issue #4): the caller supplies the sender id, so the
        # probe is gated by a dedicated bearer token that must be configured —
        # otherwise an unauthenticated caller could drive the agent under an
        # arbitrary identity (including an allowlisted approver, issue #7,
        # security #28). The webhook path is the real sender-verified channel.
        expected = f"Bearer {probe_token}"
        if not probe_token:
            raise HTTPException(
                status_code=503, detail="roundtrip probe is not configured"
            )
        if not hmac.compare_digest(
            request.headers.get("Authorization", ""), expected
        ):
            raise HTTPException(
                status_code=401, detail="invalid bearer token"
            )
        reply = runner.run_turn(sender_id=payload.sender_id, message=payload.message)
        return RoundTripResponse(sender_id=payload.sender_id, reply=reply)

    @app.api_route("/api/whatsapp/webhook", methods=["GET", "POST"])
    async def whatsapp_webhook(request: Request) -> Response:
        """Meta Cloud API's WhatsApp webhook (the live inbound channel, #13).

        Meta verifies the endpoint with a GET handshake — ``hub.challenge`` is
        echoed only when ``hub.verify_token`` matches the configured
        ``META_VERIFY_TOKEN`` — and signs every POST with
        ``X-Hub-Signature-256`` (HMAC-SHA256 of the raw body with the App
        Secret); both mechanisms are owned by the Meta adapter behind the
        ``WhatsAppWebhookParser`` seam, so the routing layer stays
        provider-free. The parsed messages are provider-neutral (E.164 sender,
        text body, media ids); every message in a delivered batch is processed,
        so a multi-message POST cannot drop an order.

        A message carrying a photo (issue #11) fetches the first media id
        through the ``MediaFetcher`` seam — now the Meta Graph API (issue #13)
        — and passes it to the agent as an inline image, understood in the same
        Gemini call as text. The agent's confirmation reply — including the
        estimated total from draft pricing — is delivered back over WhatsApp
        through the ``WhatsAppSender`` seam. A media fetch that fails falls
        back to handling the text the message carried, rather than failing the
        whole request. The endpoint acknowledges with a plain 200; anything
        with a missing or invalid signature is rejected with 403 before it is
        parsed or committed, and an oversized body is refused with 413 before
        it is buffered.
        """
        challenge = parser.verification_challenge(
            method=request.method,
            query={key: value for key, value in request.query_params.items()},
        )
        if challenge is not None:
            return Response(content=challenge, media_type="text/plain")
        # Read the body in bounded chunks: the endpoint is signature-gated, not
        # network-gated, so an oversized POST must be refused before it is
        # buffered (CWE-400), not verified against an already-read body.
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_WEBHOOK_BODY_BYTES:
                return Response(status_code=413)
            chunks.append(chunk)
        raw_body = b"".join(chunks)
        if not parser.verify_signature(
            method=request.method,
            url=str(request.url),
            headers={key: value for key, value in request.headers.items()},
            body=raw_body,
        ):
            return Response(status_code=403)
        for message in parser.parse(method=request.method, body=raw_body):
            if not message.body and not message.media:
                continue
            # Per-sender quota before any media fetch or agent turn (security
            # #32): the signature proves the sender is real, not that it is
            # well-intentioned, so a flooded number must not burn an unbounded
            # chain of Gemini turns. The limiter is in-memory per instance —
            # exact for the single-instance demo deploy.
            if not rate_limiter.allow(message.sender):
                return Response(status_code=429, content="rate limit exceeded")
            media = fetcher.fetch(message.media[0]) if message.media else None
            reply = runner.run_turn(
                sender_id=message.sender, message=message.body, media=media
            )
            if reply:
                sender.send(message.sender, reply)
        return _meta_ack()

    @app.post("/api/voice/ingest")
    async def voice_ingest(request: Request) -> Response:
        """Company-recorded call ingestion (issue #35).

        The company's own system feeds the day's recorded calls here: an audio
        body plus the caller's E.164 number, posted with a per-deploy bearer
        token. The audio is passed to the same ADK agent as inline audio with
        the same ``VOICE_NUDGE`` the recording-status callback used (issue #10),
        so a voice order with a missing field escalates as flagged and is never
        clarified (ADR-0004).

        The caller is taken from the token-authenticated payload, never from
        the message — trusted company metadata, not caller-ID. The token is
        verified before the body is read or anything parsed; the body is read
        in bounded chunks (8 MiB cap, 413 on overflow); the caller must be a
        whole-string E.164 number; the audio is base64 and capped at the same
        5 MiB as fetched media. A missing or wrong token is rejected with 401
        (and the endpoint is closed with 503 when no token is configured).
        There is no outbound voice channel, so the agent's reply is not
        delivered anywhere.
        """
        expected = f"Bearer {ingest_token}"
        if not ingest_token:
            return Response(
                status_code=503, content="voice ingest is not configured"
            )
        if not hmac.compare_digest(
            request.headers.get("Authorization", ""), expected
        ):
            return Response(status_code=401)
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_VOICE_INGEST_BODY_BYTES:
                return Response(status_code=413)
            chunks.append(chunk)
        raw_body = b"".join(chunks)
        try:
            payload = json.loads(raw_body)
        except (ValueError, TypeError):
            return Response(status_code=400)
        if not isinstance(payload, dict):
            return Response(status_code=400)
        caller = payload.get("caller")
        if not isinstance(caller, str) or not _valid_e164(caller):
            return Response(status_code=400)
        audio_b64 = payload.get("audio_base64")
        if not isinstance(audio_b64, str):
            return Response(status_code=400)
        try:
            audio = base64.b64decode(audio_b64, validate=True)
        except (ValueError, TypeError):
            return Response(status_code=400)
        if not audio:
            return Response(status_code=400)
        if len(audio) > MAX_MEDIA_BYTES:
            return Response(status_code=413)
        mime = payload.get("mime_type", "audio/wav")
        if not isinstance(mime, str) or mime not in AUDIO_MIME_TYPES:
            return Response(status_code=400)
        runner.run_turn(
            sender_id=caller,
            message=VOICE_NUDGE,
            media=MediaObject(data=audio, mime_type=mime),
        )
        return Response(content="OK", media_type="text/plain")

    if store is not None:
        late_notifier = LateOrderNotifier(store, sender)
        storage = voucher_storage or default_voucher_storage(settings.voucher_bucket)
        _register_review_routes(
            app,
            store,
            passcode,
            passcode_salt,
            cookie_secure,
            late_notifier,
            storage,
        )
        _register_loading_routes(app, store, passcode, passcode_salt, cookie_secure)
        _register_cutoff_endpoint(app, store)

    return app


def _passcode_digest(passcode: str, salt: str) -> str:
    """Deterministic token proving knowledge of a web-view passcode.

    The cookie carries this digest, never the passcode itself; the gate
    recomputes it from the configured passcode + salt and compares constant-
    time. The salt is a per-deploy secret, so the digest cannot be forged
    offline even if the passcode leaks (security #27).
    """
    return hmac.new(salt.encode(), passcode.encode(), hashlib.sha256).hexdigest()


def _as_number(value: str) -> float | None:
    value = value.strip()
    return float(value) if value else None


def _parse_day(value: str | None) -> date | None:
    """Parse a delivery-day query/param, rejecting malformed input with 400.

    A bad day string (e.g. ``?day=not-a-date``) currently surfaces as an
    uncaught 500; turn it into a clean client error instead.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid delivery day") from None


def _parse_edit_form(form) -> dict:
    """Turn the order-edit form fields into the core's ``edit_order`` changes.

    Text fields always submit (blank clears); the resolve selects and the item
    rows only submit when the approver used them. Item rows whose product is
    blank are dropped (deleted lines / unused extra rows).
    """
    changes: dict = {
        "customer": str(form.get("customer", "")).strip(),
        "delivery_location": str(form.get("delivery_location", "")).strip(),
        "gst_override_pct": _as_number(str(form.get("gst_override_pct", ""))),
    }
    customer_id = str(form.get("customer_id", "")).strip()
    if customer_id:
        changes["customer_id"] = customer_id

    items: list[dict] = []
    resolutions: list[dict] = []
    index = 0
    while f"items[{index}][product]" in form:
        product = str(form.get(f"items[{index}][product]", "")).strip()
        if product:
            items.append(
                {
                    "product": product,
                    "quantity": _as_number(
                        str(form.get(f"items[{index}][quantity]", ""))
                    )
                    or 0.0,
                    "unit": str(form.get(f"items[{index}][unit]", "")).strip(),
                    "rate_inr": _as_number(
                        str(form.get(f"items[{index}][rate_inr]", ""))
                    ),
                }
            )
            product_id = str(form.get(f"items[{index}][product_id]", "")).strip()
            if product_id:
                resolutions.append(
                    {"index": len(items) - 1, "product_id": product_id}
                )
        index += 1
    changes["items"] = items
    if resolutions:
        changes["product_resolutions"] = resolutions
    return changes


def _same_origin(request: Request) -> None:
    # CSRF guard (security #27 judgment call): the decision/edit/login POSTs are
    # state-changing, so reject requests that do not come from this service's own
    # origin. Browsers always send Origin on POST; a missing or mismatched header
    # is refused. The origin is matched on the request Host header (set by the
    # load balancer to the public host), not on base_url, so the scheme skew
    # behind Cloud Run's proxy (Origin https vs internal http base_url) can't
    # false-reject or false-allow.
    origin = request.headers.get("origin")
    if not origin:
        raise HTTPException(status_code=403, detail="cross-origin request")
    try:
        origin_host = urlparse(origin).hostname
    except ValueError:
        raise HTTPException(
            status_code=403, detail="cross-origin request"
        ) from None
    host = request.headers.get("host", "")
    host = host.split(":")[0] if host else ""
    if not origin_host or not host or origin_host != host:
        raise HTTPException(status_code=403, detail="cross-origin request")


def _register_review_routes(
    app: FastAPI,
    store: OrderStore,
    passcode: str,
    passcode_salt: str,
    cookie_secure: bool = True,
    late_notifier=None,
    voucher_storage: VoucherStore | None = None,
) -> None:
    """Register the passcode-gated review web view (issue #6).

    Every ``/review`` route is gated by a passcode supplied by the deployer
    (env ``WEB_PASSCODE`` + salt ``WEB_PASSCODE_SALT``, security #27); a
    request without a valid session cookie is redirected to the login page (or
    rejected, for JSON/POST endpoints). The web decision path calls
    ``approve_order_web`` on the same Order Processing Core the ADK agent uses,
    so web and WhatsApp approvals stay in sync and share one audit trail. The
    voucher actions (issue #8) run the same ``prepare_voucher`` seam the ADK
    tool uses and the same ``mark_billed`` core transition.
    """
    core = OrderProcessingCore(store)
    storage = voucher_storage or default_voucher_storage()

    async def _passcode() -> str:
        if not passcode or not passcode_salt:
            raise HTTPException(
                status_code=503, detail="review view is not configured"
            )
        return passcode

    async def _authorized(request: Request) -> bool:
        expected = _passcode_digest(await _passcode(), passcode_salt)
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
        request: Request,
        order_id: str,
        message: str | None = None,
        notice: str | None = None,
    ):
        await _require(request)
        order = await store.get_order(order_id)
        if order is None:
            raise HTTPException(status_code=404)
        events = await store.list_order_events(order_id)
        events.sort(key=lambda e: e.created_at)
        return review.order_page(order, events, message=message, notice=notice)

    @app.get("/review/orders/{order_id}/edit", response_class=HTMLResponse)
    async def review_edit_page(
        request: Request, order_id: str, message: str | None = None
    ):
        await _require(request)
        order = await store.get_order(order_id)
        if order is None:
            raise HTTPException(status_code=404)
        customers = await store.get_customers()
        products = await store.get_products()
        return review.edit_page(order, customers, products, message=message)

    @app.post("/review/orders/{order_id}/edit")
    async def review_edit_order(request: Request, order_id: str):
        """Apply an approver's corrections through the Order Processing Core.

        The edit goes through the same core the ADK agent uses, so web and
        WhatsApp decisions stay in sync and every change lands on the shared
        audit trail as an ``order_edited`` Order Event.
        """
        await _require(request)
        _same_origin(request)
        if await store.get_order(order_id) is None:
            raise HTTPException(status_code=404)
        form = await request.form()
        try:
            await core.edit_order(order_id, changes=_parse_edit_form(form))
        except (ApprovalError, ConfigurationError) as exc:
            return RedirectResponse(
                f"/review/orders/{quote(order_id)}?message={quote(str(exc))}",
                status_code=303,
            )
        except (ValueError, TypeError):
            return RedirectResponse(
                f"/review/orders/{quote(order_id)}?message=Could not save changes.",
                status_code=303,
            )
        return RedirectResponse(
            f"/review/orders/{quote(order_id)}?notice=Order updated.",
            status_code=303,
        )

    @app.post("/review/login", response_class=HTMLResponse)
    async def review_login(request: Request):
        _same_origin(request)
        form = await request.form()
        submitted = str(form.get("passcode", ""))
        if not hmac.compare_digest(submitted, await _passcode()):
            return review.login_page(error="Incorrect passcode.")
        response = RedirectResponse("/review", status_code=303)
        response.set_cookie(
            review.PASSCODE_COOKIE,
            _passcode_digest(await _passcode(), passcode_salt),
            httponly=True,
            samesite="lax",
            secure=cookie_secure,
            max_age=43200,
        )
        return response

    @app.post("/review/logout")
    async def review_logout(request: Request):
        _same_origin(request)
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
        _same_origin(request)
        action = "approve" if approved else "reject"
        try:
            decision = await core.approve_order_web(order_id, approved=approved)
        except Exception:
            return RedirectResponse(
                f"/review/orders/{quote(order_id)}?message="
                f"{quote(f'Could not {action} this order.')}",
                status_code=303,
            )
        if (
            late_notifier is not None
            and decision.approved
            and decision.late
        ):
            await late_notifier.on_order_late(decision.order_id)
        return RedirectResponse(
            f"/review/orders/{quote(order_id)}", status_code=303
        )

    @app.post("/review/orders/{order_id}/approve")
    async def review_approve(request: Request, order_id: str):
        return await _decide(request, order_id, approved=True)

    @app.post("/review/orders/{order_id}/reject")
    async def review_reject(request: Request, order_id: str):
        return await _decide(request, order_id, approved=False)

    @app.post("/review/orders/{order_id}/prepare-voucher")
    async def review_prepare_voucher(request: Request, order_id: str):
        """Generate an approved order's Tally voucher (issue #8).

        Runs the same ``prepare_voucher`` seam as the ADK tool, so the web
        button and the agent produce the same voucher, stored in the same
        place, recorded on the same audit trail.
        """
        await _require(request)
        _same_origin(request)
        if await store.get_order(order_id) is None:
            raise HTTPException(status_code=404, detail="order not found")
        try:
            await prepare_voucher(store, storage, order_id)
        except VoucherError as exc:
            return RedirectResponse(
                f"/review/orders/{quote(order_id)}?message={quote(str(exc))}",
                status_code=303,
            )
        return RedirectResponse(
            f"/review/orders/{quote(order_id)}?notice=Voucher prepared.",
            status_code=303,
        )

    @app.get("/review/orders/{order_id}/voucher")
    async def review_voucher_download(request: Request, order_id: str):
        """Download a prepared order's Tally voucher XML (issue #8).

        The import path is a manual download + file import into Tally; this
        route serves the stored XML as an attachment.
        """
        await _require(request)
        order = await store.get_order(order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="order not found")
        if not order.voucher_id:
            raise HTTPException(status_code=404, detail="no voucher prepared")
        xml = await storage.read(order.voucher_id)
        if xml is None:
            raise HTTPException(
                status_code=404, detail="voucher not found in storage"
            )
        # The voucher id is system-generated (``voucher_ord_<hex>``); keep only
        # safe filename characters so nothing header-breaking reaches the
        # Content-Disposition value (defense-in-depth: the id never carries
        # control characters today).
        safe_name = "".join(
            ch for ch in str(order.voucher_id) if ch.isalnum() or ch in "._-"
        )
        return Response(
            content=xml,
            media_type="application/xml",
            headers={
                "Content-Disposition": f'attachment; filename="{safe_name}.xml"'
            },
        )

    @app.post("/review/orders/{order_id}/billed")
    async def review_mark_billed(request: Request, order_id: str):
        """Mark a prepared voucher's order as billed (issue #8).

        Runs ``mark_billed`` on the same Order Processing Core, so the web
        action stays on the shared audit trail as an ``order_billed`` event.
        """
        await _require(request)
        _same_origin(request)
        if await store.get_order(order_id) is None:
            raise HTTPException(status_code=404, detail="order not found")
        try:
            await core.mark_billed(order_id)
        except ValueError:
            raise HTTPException(
                status_code=409, detail="order cannot be marked billed"
            ) from None
        return RedirectResponse(
            f"/review/orders/{quote(order_id)}", status_code=303
        )


def _register_loading_routes(
    app: FastAPI,
    store: OrderStore,
    passcode: str,
    passcode_salt: str,
    cookie_secure: bool = True,
) -> None:
    """Register the passcode-gated Loading List web view (issue #9).

    The dispatch-facing page renders the live approved orders for a delivery
    day through the same ``load_loading_list`` path as the ADK tool and the
    Cutoff endpoint. The web view is gated by the same passcode + salt that
    gate the review view (security #27); a request without a valid session
    cookie is shown the login form. Marking an order dispatched runs
    ``mark_dispatched`` on the same Order Processing Core, recording the
    ``order_dispatched`` Order Event.
    """
    core = OrderProcessingCore(store)

    async def _passcode() -> str:
        if not passcode or not passcode_salt:
            raise HTTPException(
                status_code=503, detail="loading view is not configured"
            )
        return passcode

    async def _authorized(request: Request) -> bool:
        expected = _passcode_digest(await _passcode(), passcode_salt)
        cookie = request.cookies.get(LOADING_PASSCODE_COOKIE, "")
        return bool(cookie) and hmac.compare_digest(cookie, expected)

    async def _require(request: Request) -> None:
        if not await _authorized(request):
            raise HTTPException(status_code=401)

    async def _render(delivery_day: str | None) -> str:
        day = _parse_day(delivery_day)
        loading = await load_loading_list(store, delivery_day=day)
        return render_loading_list_html(loading)

    @app.get("/loading", response_class=HTMLResponse)
    async def loading_page(request: Request, day: str | None = None):
        if not await _authorized(request):
            return loading_login_page()
        return await _render(day)

    @app.post("/loading/login", response_class=HTMLResponse)
    async def loading_login(request: Request):
        _same_origin(request)
        form = await request.form()
        submitted = str(form.get("passcode", ""))
        if not hmac.compare_digest(submitted, await _passcode()):
            return loading_login_page(error="Incorrect passcode.")
        response = RedirectResponse("/loading", status_code=303)
        response.set_cookie(
            LOADING_PASSCODE_COOKIE,
            _passcode_digest(await _passcode(), passcode_salt),
            httponly=True,
            samesite="lax",
            secure=cookie_secure,
            max_age=43200,
        )
        return response

    @app.post("/loading/logout")
    async def loading_logout(request: Request):
        _same_origin(request)
        response = RedirectResponse("/loading", status_code=303)
        response.delete_cookie(LOADING_PASSCODE_COOKIE)
        return response

    @app.post("/loading/orders/{order_id}/dispatch")
    async def loading_dispatch(request: Request, order_id: str):
        await _require(request)
        _same_origin(request)
        if await store.get_order(order_id) is None:
            raise HTTPException(status_code=404, detail="order not found")
        try:
            await core.mark_dispatched(order_id)
        except ValueError:
            raise HTTPException(
                status_code=409, detail="order cannot be dispatched"
            ) from None
        return RedirectResponse("/loading", status_code=303)


def _register_cutoff_endpoint(app: FastAPI, store: OrderStore) -> None:
    """Register the secret-gated daily Cutoff render endpoint (issue #9).

    The Cloud Scheduler job fires this endpoint after the daily cutoff; it runs
    the same ``load_loading_list`` render path as the web view and the ADK tool
    and returns the live Loading List for today as JSON (a convenience trigger,
    never required for correctness — the WhatsApp heads-up for late orders is
    sent from the intake path, not from this endpoint).

    Auth follows the scaffold's pipeline: Cloud Scheduler publishes to the
    ``valence-cutoff`` topic, whose push subscription (push-auth service
    account) delivers a Pub/Sub push envelope to this endpoint. Because the
    service is deployed ``--allow-unauthenticated``, the envelope alone proves
    nothing — so the scheduled message body carries the configured
    ``CUTOFF_SECRET`` and the endpoint verifies it constant-time, the same way
    a direct call presents it as a bearer token. The endpoint is closed (503)
    when no secret is configured, so a misdeployed job fails loudly instead of
    silently rendering without auth.
    """
    async def _authorized(request: Request) -> bool:
        if not settings.cutoff_secret:
            raise HTTPException(
                status_code=503, detail="cutoff endpoint is not configured"
            )
        # Direct invocation: bearer token.
        expected = f"Bearer {settings.cutoff_secret}"
        if hmac.compare_digest(request.headers.get("Authorization", ""), expected):
            return True
        # Pub/Sub push envelope: the message data carries the secret. The
        # endpoint is otherwise unauthenticated, so cap the body before parsing
        # an attacker-controlled payload (a real envelope is a few hundred
        # bytes; anything larger is not one).
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                too_large = int(content_length) > 64 * 1024
            except ValueError:
                too_large = True
            if too_large:
                return False
        try:
            body = await request.json()
        except Exception:
            return False
        message = body.get("message")
        data = message.get("data") if isinstance(message, dict) else None
        if not isinstance(data, str):
            return False
        try:
            payload = json.loads(base64.b64decode(data))
        except Exception:
            return False
        submitted = payload.get("secret") if isinstance(payload, dict) else None
        return hmac.compare_digest(str(submitted), settings.cutoff_secret)

    @app.post("/api/cutoff")
    async def cutoff_render(request: Request, day: str | None = None):
        if not await _authorized(request):
            raise HTTPException(status_code=401)
        delivery_day = _parse_day(day)
        loading = await load_loading_list(store, delivery_day=delivery_day)
        return loading.to_dict()
