"""Shared Twilio webhook request verification (``X-Twilio-Signature``).

Every Twilio webhook — the WhatsApp "When a message comes in" callback (issue
#4) and the Voice recording-status callback (issue #10) — signs its request
with the same algorithm: take the full request URL, append each POST parameter
sorted by name as ``keyvalue`` with no delimiters, HMAC-SHA1 with the Auth
Token as the key, and base64-encode. Both channel adapters verify against this
one implementation.
"""

from __future__ import annotations

import base64
import hashlib
import hmac


def build_twilio_signature(
    url: str, params: dict[str, str], auth_token: str
) -> str:
    """Compute the ``X-Twilio-Signature`` for a webhook request.

    Algorithm (Twilio docs, "Validate Twilio requests"): take the full request
    URL, append each POST parameter sorted by name as ``keyvalue`` with no
    delimiters, HMAC-SHA1 the result with the AuthToken as the key, and
    base64-encode it.
    """
    canonical = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    digest = hmac.new(
        auth_token.encode(), canonical.encode(), hashlib.sha1
    ).digest()
    return base64.b64encode(digest).decode()


def verify_twilio_signature(
    url: str, params: dict[str, str], signature: str, auth_token: str
) -> bool:
    """True when ``signature`` is a valid ``X-Twilio-Signature`` for the request.

    A missing signature header, or an empty auth token (unconfigured env), is
    always rejected.
    """
    if not signature or not auth_token:
        return False
    expected = build_twilio_signature(url, params, auth_token)
    return hmac.compare_digest(expected, signature)
