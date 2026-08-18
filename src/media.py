"""The media boundary for channel media (photos and call recordings).

Retrieving channel media requires provider credentials: Meta media (issue #13)
uses a media ``id`` fetched through the Graph API with a bearer token. The
retriever lives behind the ``MediaFetcher`` seam, returning a neutral
``MediaObject`` (bytes + mime type) so the webhook and agent never touch the
provider. A failed or unauthenticated fetch returns ``None``; the photo webhook
then falls back to handling whatever text the message carried. Call recordings
travel differently — the company-recorded ingest path (issue #35) carries the
audio in the request body, so no fetch is involved there.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from google.genai import types

from .meta_whatsapp import GRAPH_API_BASE, is_valid_media_id


@dataclass(frozen=True)
class MediaObject:
    """Provider-neutral media content: raw bytes plus a mime type."""

    data: bytes
    mime_type: str


class MediaFetcher(Protocol):
    """Inbound seam: retrieve the content of a provider media reference.

    ``reference`` is provider-neutral — whatever the ``InboundMessage`` carried
    (a Meta media id today). Returns ``None`` when the fetch cannot complete
    (network error, missing credentials, or a non-success response) so the
    caller can fall back rather than fail the whole request.
    """

    def fetch(self, reference: str) -> MediaObject | None: ...


# Media is an image for the model; cap the read so an oversized or looping
# response cannot exhaust memory.
MAX_MEDIA_BYTES = 5 * 1024 * 1024  # 5 MiB

# The audio mime types the voice-ingest endpoint (issue #35) will attach to
# inline audio. Both ``audio/mpeg`` and ``audio/mp3`` name MP3 — Gemini accepts
# either — so a feed can use its own convention. The map below is the single
# source of truth for both the endpoint and the feed script.
AUDIO_MIME_TYPES: frozenset[str] = frozenset(
    {"audio/wav", "audio/mpeg", "audio/mp3", "audio/mp4", "audio/amr", "audio/ogg"}
)

# Mime type a recorded-call feed (scripts/feed_voice.py) attaches by extension.
AUDIO_MIME_BY_EXTENSION: dict[str, str] = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".amr": "audio/amr",
    ".ogg": "audio/ogg",
}


def audio_mime_for_name(name: str) -> str | None:
    """The accepted audio mime for a filename's extension, else ``None``."""
    return AUDIO_MIME_BY_EXTENSION.get(Path(name).suffix.lower())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect — a response from an allowlisted host must not
    redirect the fetch (and the credentials) on to another host (CWE-918)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


class MetaMediaFetcher:
    """Fetch a Meta media id through the Graph API with a bearer token.

    The reference must be a bare media id — never a URL or a path-like value
    (SSRF / credential-leak guard, CWE-918: the access token must never leave
    ``graph.facebook.com``). The Graph API host is fixed, redirects are never
    followed, and the body is size-capped. ``_open`` is the seam tests patch to
    fake the HTTP round trip; production uses ``urllib``.
    """

    def __init__(self, access_token: str) -> None:
        self._access_token = access_token
        self._open = self._default_open

    def _default_open(self, request: urllib.request.Request):
        opener = urllib.request.build_opener(_NoRedirect)
        return opener.open(request, timeout=30)

    def fetch(self, reference: str) -> MediaObject | None:
        if not self._access_token or not is_valid_media_id(reference):
            return None
        url = f"{GRAPH_API_BASE}/{reference}"
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self._access_token}"}
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


def media_to_inline_part(media: MediaObject) -> types.Part:
    """Wrap a ``MediaObject`` as an ADK/genai inline-data part."""
    return types.Part(
        inline_data=types.Blob(data=media.data, mime_type=media.mime_type)
    )
