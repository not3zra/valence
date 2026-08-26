"""Meta Cloud API WhatsApp adapter: parsing, verification, and the outbound sender.

Everything Meta-specific about the WhatsApp channel lives here, behind the seam
defined in ``src.whatsapp`` (issue #4 design note, issue #13): parsing turns
Meta's nested JSON webhook (``entry`` → ``changes`` → ``value`` → ``messages``)
into a provider-neutral ``InboundMessage``, and webhook verification owns the
two Meta mechanisms — the GET verification handshake (``hub.mode`` /
``hub.verify_token`` / ``hub.challenge``) and the ``X-Hub-Signature-256``
header (HMAC-SHA256 of the raw request body with the App Secret, hex). The
outbound ``MetaWhatsAppSender`` POSTs text replies to the Graph API
``/messages`` endpoint with a permanent access token.

Provider specifics stay in this module: media travels as a Meta media ``id``,
not a URL, so the webhook and agent only ever see a neutral reference that the
``MediaFetcher`` seam resolves (issue #11 / #13). The Graph API host is fixed
and requests never follow redirects, so an attacker-controlled payload cannot
turn the sender or the media fetch into an SSRF or leak the access token.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import urllib.error
import urllib.request

from .whatsapp import InboundMessage

# Meta's Graph API version and the messages/media endpoint host.
GRAPH_API_VERSION = "v20.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

# Meta posts a message field per media type; each carries an ``id``. The list
# covers every media a customer could attach to an order (photo intake is
# ticket #11): the first is what the webhook fetches through the MediaFetcher.
_MEDIA_FIELDS = ("image", "video", "document", "audio", "sticker")


def build_meta_signature(body: bytes, app_secret: str) -> str:
    """Compute the ``X-Hub-Signature-256`` for a webhook request body.

    Algorithm (Meta docs, "Verify webhook deliveries"): HMAC-SHA256 of the raw
    request body with the App Secret, hex-encoded, prefixed ``sha256=``.
    """
    digest = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_meta_signature(body: bytes, signature: str, app_secret: str) -> bool:
    """True when ``signature`` is a valid ``X-Hub-Signature-256`` for the body.

    A missing signature header, or an empty app secret (unconfigured env), is
    always rejected.
    """
    if not signature or not app_secret:
        return False
    return hmac.compare_digest(build_meta_signature(body, app_secret), signature)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect — the fixed Graph API host must not forward the
    access token (or the send) on to another host (CWE-918)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


class MetaWhatsAppParser:
    """Own a Meta webhook's verification handshake, signature, and parsing.

    ``app_secret`` and ``verify_token`` are the configured Meta secrets
    (``META_APP_SECRET`` / ``META_VERIFY_TOKEN``); an empty value fails closed
    (handshake and signatures never verify). Parsing reads the nested Meta JSON
    shape into a neutral ``InboundMessage``, normalizing the E.164 ``from``
    (Meta sends it without the leading ``+``) and passing media ids through.
    """

    def __init__(self, app_secret: str, verify_token: str) -> None:
        self._app_secret = app_secret
        self._verify_token = verify_token

    def verification_challenge(
        self, *, method: str, query: dict[str, str]
    ) -> str | None:
        """Return Meta's ``hub.challenge`` for a valid GET handshake, else None.

        Meta verifies the endpoint by GETting it with ``hub.mode=subscribe``,
        ``hub.verify_token``, and ``hub.challenge``; the endpoint must echo the
        challenge. The token comparison is constant-time and fails closed when
        no verify token is configured.
        """
        if method != "GET" or not self._verify_token:
            return None
        if query.get("hub.mode") != "subscribe":
            return None
        submitted = query.get("hub.verify_token") or ""
        if not hmac.compare_digest(submitted, self._verify_token):
            return None
        return query.get("hub.challenge")

    def verify_signature(
        self, *, method: str, url: str, headers: dict[str, str], body: bytes
    ) -> bool:
        """Verify the ``X-Hub-Signature-256`` header over the raw body.

        The header key is matched case-insensitively — HTTP servers and proxies
        lower-case header names in transit.
        """
        if method != "POST":
            return False
        signature = next(
            (
                value
                for key, value in headers.items()
                if key.lower() == "x-hub-signature-256"
            ),
            "",
        )
        return verify_meta_signature(body, signature, self._app_secret)

    def parse(self, *, method: str, body: bytes) -> list[InboundMessage]:
        """Turn Meta's nested webhook JSON into neutral ``InboundMessage`` objects.

        Meta may deliver several messages in one webhook POST, so every message
        in the batch is returned and the caller processes them all — dropping
        any would lose an order with no retry (the endpoint acks 200). Empty
        when the request carried no message to process (a status/echo callback,
        a non-POST, or a malformed body).
        """
        if method != "POST":
            return []
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return []
        messages: list[InboundMessage] = []
        for entry in payload.get("entry", []) if isinstance(payload, dict) else []:
            changes = entry.get("changes", []) if isinstance(entry, dict) else []
            for change in changes:
                if not isinstance(change, dict):
                    continue
                value = change.get("value")
                if not isinstance(value, dict):
                    continue
                for message in value.get("messages", []):
                    if not isinstance(message, dict):
                        continue
                    sender = str(message.get("from") or "").strip()
                    if not sender:
                        continue
                    if not sender.startswith("+"):
                        sender = f"+{sender}"
                    body_text = message.get("text")
                    text = (
                        body_text.get("body")
                        if isinstance(body_text, dict)
                        else ""
                    )
                    messages.append(
                        InboundMessage(
                            sender=sender,
                            body=str(text or ""),
                            media=self._media_ids(message),
                        )
                    )
        return messages

    @staticmethod
    def _media_ids(message: dict) -> tuple[str, ...]:
        ids: list[str] = []
        for kind in _MEDIA_FIELDS:
            media = message.get(kind)
            if isinstance(media, dict) and media.get("id"):
                ids.append(str(media["id"]))
        return tuple(ids)


class MetaWhatsAppSender:
    """Deliver a WhatsApp text reply through the Meta Graph API.

    POSTs to ``/v20.0/{PHONE_NUMBER_ID}/messages`` with a permanent access
    token. Delivery is best-effort: a failure to send must never surface as a
    webhook error, since the confirmation reply is out-of-band (issue #4
    design note). ``_open`` is the seam tests patch to fake the HTTP round
    trip; production uses ``urllib`` with redirects refused so the token never
    leaves the Graph API host.
    """

    def __init__(self, access_token: str, phone_number_id: str) -> None:
        self._access_token = access_token
        self._phone_number_id = phone_number_id
        self._open = self._default_open

    def _default_open(self, request: urllib.request.Request):
        opener = urllib.request.build_opener(_NoRedirect)
        return opener.open(request, timeout=30)

    def send(self, recipient: str, text: str) -> None:
        if not self._access_token or not self._phone_number_id:
            return
        url = f"{GRAPH_API_BASE}/{self._phone_number_id}/messages"
        payload = json.dumps(
            {
                "messaging_product": "whatsapp",
                "to": recipient,
                "type": "text",
                "text": {"body": text},
            }
        ).encode()
        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with self._open(request) as response:
                response.read()
        except (OSError, urllib.error.HTTPError, urllib.error.URLError) as exc:
            import logging
            logging.warning("MetaWhatsAppSender.send FAILED: %s %s", type(exc).__name__, exc)
            return


# A media id must be a bare identifier — never a URL or a path-like value — so
# an attacker-controlled webhook id cannot point the server-side Graph API
# fetch at another host (SSRF, CWE-918) or leak the access token.
MEDIA_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


def is_valid_media_id(reference: str) -> bool:
    """True when ``reference`` looks like a Meta media id, not a URL/path."""
    return bool(reference) and MEDIA_ID_PATTERN.fullmatch(reference) is not None
