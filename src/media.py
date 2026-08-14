"""The media boundary for channel media (photos and call recordings).

Twilio WhatsApp media and Voice recordings are not public: retrieving them
requires HTTP basic auth (the Account SID as username and the Auth Token as
password). That fetch lives behind the ``MediaFetcher`` seam, returning a
neutral ``MediaObject`` (bytes + mime type) so the webhook and agent never
touch the provider. A failed or unauthenticated fetch returns ``None``; the
photo webhook then falls back to handling whatever text the message carried,
and the voice webhook acknowledges the callback so Twilio can retry.
"""

from __future__ import annotations

import base64
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

from google.genai import types


@dataclass(frozen=True)
class MediaObject:
    """Provider-neutral media content: raw bytes plus a mime type."""

    data: bytes
    mime_type: str


class MediaFetcher(Protocol):
    """Inbound seam: retrieve the content of a provider media URL.

    Returns ``None`` when the fetch cannot complete (network error, missing
    credentials, or a non-success response) so the caller can fall back rather
    than fail the whole request.
    """

    def fetch(self, url: str) -> MediaObject | None: ...


# Only these hosts may be fetched — Twilio serves WhatsApp media from its own
# domains. Anything else is rejected outright, so an attacker who controls the
# webhook's MediaUrlN value cannot point the server-side fetch at an arbitrary
# host (SSRF, CWE-918) or leak the Auth Token to a host they control.
ALLOWED_MEDIA_HOSTS: frozenset[str] = frozenset(
    {"api.twilio.com", "media.twilio.com"}
)

# Media is an image for the model; cap the read so an oversized or looping
# response cannot exhaust memory.
MAX_MEDIA_BYTES = 5 * 1024 * 1024  # 5 MiB


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect — a response from an allowlisted host must not
    redirect the fetch (and the credentials) on to another host (CWE-918)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


class TwilioMediaFetcher:
    """Fetch a Twilio media object using basic auth (Account SID : Auth Token).

    The URL must be ``https`` on an allowlisted Twilio host, redirects are not
    followed, and the body is capped, so an attacker-controlled ``MediaUrlN``
    cannot turn the fetch into SSRF, credential leakage, or a memory blow-up.
    ``_open`` is the seam tests patch to fake the HTTP round trip; production
    uses ``urllib``.
    """

    def __init__(self, account_sid: str, auth_token: str) -> None:
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._open = self._default_open

    def _default_open(self, request: urllib.request.Request):
        opener = urllib.request.build_opener(_NoRedirect)
        return opener.open(request, timeout=30)

    def fetch(self, url: str) -> MediaObject | None:
        if not self._allowed(url) or not self._account_sid or not self._auth_token:
            return None
        credentials = base64.b64encode(
            f"{self._account_sid}:{self._auth_token}".encode()
        ).decode()
        request = urllib.request.Request(
            url, headers={"Authorization": f"Basic {credentials}"}
        )
        try:
            with self._open(request) as response:
                data = response.read(MAX_MEDIA_BYTES + 1)
                if len(data) > MAX_MEDIA_BYTES:
                    return None
                content_type = response.headers.get("Content-Type", "")
                mime = content_type.split(";")[0].strip() or "application/octet-stream"
                return MediaObject(data=data, mime_type=mime)
        except (OSError, urllib.error.HTTPError, urllib.error.URLError):
            return None

    def _allowed(self, url: str) -> bool:
        """True only for https URLs on an allowlisted Twilio media host."""
        try:
            parsed = urlparse(url)
        except ValueError:
            return False
        if parsed.scheme != "https" or not parsed.hostname:
            return False
        return parsed.hostname in ALLOWED_MEDIA_HOSTS


def media_to_inline_part(media: MediaObject) -> types.Part:
    """Wrap a ``MediaObject`` as an ADK/genai inline-data part."""
    return types.Part(
        inline_data=types.Blob(data=media.data, mime_type=media.mime_type)
    )
