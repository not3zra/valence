"""Channel media retrieval behind the ``MediaFetcher`` seam (issues #11 / #13).

One fetcher sits behind the seam today: ``MetaMediaFetcher`` retrieves Meta
media ids through the Graph API with a bearer token. It validates the
reference before fetching (SSRF / credential-leak guard), never follows
redirects, and size-caps the body. A fetch that fails validation, errors, or
is oversized returns ``None`` so the webhook can fall back instead of crashing.
"""

from __future__ import annotations

from src.media import MediaObject, MetaMediaFetcher

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


