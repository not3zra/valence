"""Channel media retrieval behind the ``MediaFetcher`` seam (issues #11 / #13).

Two fetchers sit behind the one seam: ``TwilioMediaFetcher`` retrieves Twilio
media (WhatsApp photo URLs for the retired channel, Voice recording URLs) with
basic auth from an allowlisted Twilio host; ``MetaMediaFetcher`` retrieves Meta
media ids through the Graph API with a bearer token. Both validate the
reference before fetching (SSRF / credential-leak guard), never follow
redirects, and size-cap the body. A fetch that fails validation, errors, or is
oversized returns ``None`` so the webhook can fall back instead of crashing.
"""

from __future__ import annotations

import base64

from src.media import MediaObject, MetaMediaFetcher, TwilioMediaFetcher

ACCESS_TOKEN = "EAA-test-access-token"


def _fake_open(fetcher, captured=None):
    """Install a fake ``_open`` on ``fetcher`` that returns the given bytes."""

    def fake_open(request):
        if captured is not None:
            captured["request"] = request
        return FakeResponse()

    class FakeResponse:
        headers = {"Content-Type": "image/jpeg"}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, limit=-1) -> bytes:
            return b"\xff\xd8fake-jpeg"

    fetcher._open = fake_open  # type: ignore[attr-defined]
    return captured


# --- Meta media-id retrieval via the Graph API (issue #13) -------------------


def test_meta_fetcher_hits_graph_api_with_bearer_token():
    fetcher = MetaMediaFetcher(ACCESS_TOKEN)
    captured: dict = {}
    _fake_open(fetcher, captured)
    result = fetcher.fetch("1234567890")

    assert result == MediaObject(data=b"\xff\xd8fake-jpeg", mime_type="image/jpeg")
    request = captured["request"]
    assert request.full_url == "https://graph.facebook.com/v20.0/1234567890"
    assert request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"


def test_meta_fetcher_returns_none_without_access_token():
    fetcher = MetaMediaFetcher("")
    assert fetcher.fetch("1234567890") is None


def test_meta_fetcher_rejects_urls_and_paths():
    # Only a bare media id is ever fetched — a URL or path-like reference would
    # let an attacker redirect the Graph API fetch (SSRF, CWE-918) or make the
    # bearer token leave Meta's host.
    fetcher = MetaMediaFetcher(ACCESS_TOKEN)
    for reference in (
        "",
        "https://evil.example/steal",
        "http://169.254.169.254/latest/meta-data",
        "https://graph.facebook.com/v20.0/123",
        "..%2F..%2Fetc%2Fpasswd",
        "123/456",
        "https://graph.facebook.com/123?x=1",
    ):
        assert fetcher.fetch(reference) is None, reference


def test_meta_fetcher_rejects_oversized_body():
    fetcher = MetaMediaFetcher(ACCESS_TOKEN)

    class HugeResponse:
        headers = {"Content-Type": "image/jpeg"}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, limit=-1) -> bytes:
            from src.media import MAX_MEDIA_BYTES

            return b"\xff" * (MAX_MEDIA_BYTES + 1)

    def fake_open(request):
        return HugeResponse()

    fetcher._open = fake_open  # type: ignore[attr-defined]
    assert fetcher.fetch("1234567890") is None


def test_meta_fetcher_returns_none_on_network_error():
    fetcher = MetaMediaFetcher(ACCESS_TOKEN)

    def fake_open(request):
        raise OSError("boom")

    fetcher._open = fake_open  # type: ignore[attr-defined]
    assert fetcher.fetch("1234567890") is None


def test_meta_fetcher_defaults_mime_when_header_missing():
    fetcher = MetaMediaFetcher(ACCESS_TOKEN)

    class FakeResponse:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, limit=-1) -> bytes:
            return b"data"

    def fake_open(request):
        return FakeResponse()

    fetcher._open = fake_open  # type: ignore[attr-defined]
    result = fetcher.fetch("1234567890")
    assert result is not None
    assert result.mime_type == "application/octet-stream"


# --- Twilio media retrieval (issue #11) --------------------------------------


def test_fetcher_returns_bytes_and_mime_for_success():
    # A 200 response carrying an image returns its bytes and content type.
    fetcher = TwilioMediaFetcher(account_sid="ACxxxx", auth_token="tok")

    class FakeResponse:
        headers = {"Content-Type": "image/jpeg"}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, limit=-1) -> bytes:
            return b"\xff\xd8fake-jpeg"

    def fake_open(request):
        assert request.headers["Authorization"] == "Basic QUN4eHh4OnRvaw=="
        return FakeResponse()

    fetcher._open = fake_open  # type: ignore[attr-defined]
    result = fetcher.fetch("https://api.twilio.com/.../Media/ME1")

    assert result == MediaObject(data=b"\xff\xd8fake-jpeg", mime_type="image/jpeg")


def test_fetcher_sends_basic_auth_credentials():
    fetcher = TwilioMediaFetcher(account_sid="ACabc", auth_token="secret")

    captured = {}

    class FakeResponse:
        headers = {"Content-Type": "image/png"}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, limit=-1) -> bytes:
            return b"png"

    def fake_open(request):
        captured["authorization"] = request.headers.get("Authorization")
        return FakeResponse()

    fetcher._open = fake_open  # type: ignore[attr-defined]
    fetcher.fetch("https://api.twilio.com/photo.png")

    expected = base64.b64encode(b"ACabc:secret").decode()
    assert captured["authorization"] == f"Basic {expected}"


def test_fetcher_returns_none_without_credentials():
    fetcher = TwilioMediaFetcher(account_sid="", auth_token="")
    assert fetcher.fetch("https://api.twilio.com/photo.png") is None


def test_fetcher_rejects_non_allowlisted_host():
    fetcher = TwilioMediaFetcher(account_sid="ACxxxx", auth_token="tok")
    # Any host outside api.twilio.com / media.twilio.com is refused — an
    # attacker-controlled MediaUrlN cannot point the fetch at an internal or
    # arbitrary host (SSRF, CWE-918), and the Auth Token never leaves Twilio.
    for url in (
        "http://169.254.169.254/latest/meta-data",
        "https://evil.example/steal",
        "https://metadata.google.internal/",
        "http://api.twilio.com/photo",  # http, not https
        "https://api.twilio.com.evil.example/photo",
    ):
        assert fetcher.fetch(url) is None


def test_fetcher_rejects_oversized_body():
    fetcher = TwilioMediaFetcher(account_sid="ACxxxx", auth_token="tok")

    class HugeResponse:
        headers = {"Content-Type": "image/jpeg"}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, limit=-1) -> bytes:
            from src.media import MAX_MEDIA_BYTES

            return b"\xff" * (MAX_MEDIA_BYTES + 1)

    def fake_open(request):
        return HugeResponse()

    fetcher._open = fake_open  # type: ignore[attr-defined]
    assert fetcher.fetch("https://api.twilio.com/photo.jpg") is None


def test_fetcher_returns_none_on_network_error():
    fetcher = TwilioMediaFetcher(account_sid="ACxxxx", auth_token="tok")

    def fake_open(request):
        raise OSError("boom")

    fetcher._open = fake_open  # type: ignore[attr-defined]
    assert fetcher.fetch("https://api.twilio.com/photo.png") is None


def test_fetcher_defaults_mime_when_header_missing():
    fetcher = TwilioMediaFetcher(account_sid="ACxxxx", auth_token="tok")

    class FakeResponse:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, limit=-1) -> bytes:
            return b"data"

    def fake_open(request):
        return FakeResponse()

    fetcher._open = fake_open  # type: ignore[attr-defined]
    result = fetcher.fetch("https://api.twilio.com/photo")
    assert result is not None
    assert result.data == b"data"
    assert result.mime_type == "application/octet-stream"
