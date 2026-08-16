"""Voice intake over the recording-status callback (issue #10).

Twilio Voice fires a recording-status callback when a call recording completes.
The webhook verifies the shared Twilio signature, fetches the recording from
the Twilio URL through the ``MediaFetcher`` seam, and passes it to the same ADK
agent as inline audio — understood through the same Gemini call as text, one
agent, a second channel. A voice order with a missing field escalates as
flagged: it never waits on a clarifying question (ADR-0004).
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from google.adk.sessions import InMemorySessionService

from src.agent import build_agent
from src.media import MediaObject
from src.store import InMemoryOrderStore
from src.twilio import build_twilio_signature
from src.web import create_app

from .fakes import FakeMediaFetcher, VoiceMissingFieldLlm, VoiceReadingLlm

CALLBACK_URL = "/api/voice/callback"
AUTH_TOKEN = "test-auth-token"

# The callback's RecordingUrl points at the recording resource (no media
# extension); the adapter resolves it to an audio URL (…/Recordings/RE.wav)
# that the MediaFetcher is then asked to retrieve.
RECORDING_URL = (
    "https://api.twilio.com/2010-04-01/Accounts/ACxxxx/Recordings/RErecording01"
)
AUDIO_URL = RECORDING_URL + ".wav"


def _sign(form: dict[str, str]) -> str:
    return build_twilio_signature(f"http://testserver{CALLBACK_URL}", form, AUTH_TOKEN)


def _callback(*, status: str = "completed", caller: str = "+919812345001") -> dict:
    return {
        "CallSid": "CA11111111111111111111111111111111",
        "From": caller,
        "To": "+14159998888",
        "RecordingSid": "RErecording01",
        "RecordingUrl": RECORDING_URL,
        "RecordingStatus": status,
        "Duration": "42",
    }


def test_voice_callback_understands_recording_and_commits_order():
    store = InMemoryOrderStore()
    fetcher = FakeMediaFetcher(
        media=MediaObject(data=b"RIFF fake-wav", mime_type="audio/wav")
    )
    app = create_app(
        agent=build_agent(model=VoiceReadingLlm(), store=store),
        session_service=InMemorySessionService(),
        voice_media_fetcher=fetcher,
        twilio_auth_token=AUTH_TOKEN,
    )
    client = TestClient(app)

    form = _callback()
    response = client.post(
        CALLBACK_URL, data=form, headers={"X-Twilio-Signature": _sign(form)}
    )
    assert response.status_code == 200
    assert "<Response" in response.text

    assert fetcher.requested_refs == [AUDIO_URL]
    assert len(store.orders) == 1
    order = store.orders[0]
    assert order.phone == "+919812345001"
    assert order.source_channel == "voice"
    assert order.status.value == "approved"


def test_voice_callback_missing_field_escalates_never_clarifies():
    store = InMemoryOrderStore()
    fetcher = FakeMediaFetcher(
        media=MediaObject(data=b"RIFF fake-wav", mime_type="audio/wav")
    )
    app = create_app(
        agent=build_agent(model=VoiceMissingFieldLlm(), store=store),
        session_service=InMemorySessionService(),
        voice_media_fetcher=fetcher,
        twilio_auth_token=AUTH_TOKEN,
    )
    client = TestClient(app)

    form = _callback()
    response = client.post(
        CALLBACK_URL, data=form, headers={"X-Twilio-Signature": _sign(form)}
    )
    assert response.status_code == 200

    # The voice order is escalated as flagged — it is never held for a
    # clarifying question (ADR-0004), and never silently approved.
    assert len(store.orders) == 1
    order = store.orders[0]
    assert order.status.value == "pending_review"
    assert "missing_field" in order.escalation_reasons


def test_voice_callback_ignores_non_completed_recording():
    store = InMemoryOrderStore()
    fetcher = FakeMediaFetcher(
        media=MediaObject(data=b"RIFF fake-wav", mime_type="audio/wav")
    )
    app = create_app(
        agent=build_agent(model=VoiceReadingLlm(), store=store),
        session_service=InMemorySessionService(),
        voice_media_fetcher=fetcher,
        twilio_auth_token=AUTH_TOKEN,
    )
    client = TestClient(app)

    form = _callback(status="in-progress")
    response = client.post(
        CALLBACK_URL, data=form, headers={"X-Twilio-Signature": _sign(form)}
    )
    assert response.status_code == 200
    assert fetcher.requested_refs == []
    assert store.orders == []


def test_voice_callback_rejects_invalid_signature():
    store = InMemoryOrderStore()
    app = create_app(
        agent=build_agent(model=VoiceReadingLlm(), store=store),
        session_service=InMemorySessionService(),
        twilio_auth_token=AUTH_TOKEN,
    )
    client = TestClient(app)

    form = _callback()
    response = client.post(
        CALLBACK_URL, data=form, headers={"X-Twilio-Signature": "bogus"}
    )
    assert response.status_code == 403
    assert store.orders == []


def test_voice_callback_fetch_failure_acks_without_processing():
    store = InMemoryOrderStore()
    fetcher = FakeMediaFetcher(media=None)
    app = create_app(
        agent=build_agent(model=VoiceReadingLlm(), store=store),
        session_service=InMemorySessionService(),
        voice_media_fetcher=fetcher,
        twilio_auth_token=AUTH_TOKEN,
    )
    client = TestClient(app)

    form = _callback()
    response = client.post(
        CALLBACK_URL, data=form, headers={"X-Twilio-Signature": _sign(form)}
    )
    assert response.status_code == 500
    assert fetcher.requested_refs == [AUDIO_URL]
    # There is no message text to fall back to on the voice channel, so a
    # failed fetch returns an error status and Twilio retries the
    # recording-status callback, rather than committing a fabricated order or
    # losing a transiently-failing retrieval.
    assert store.orders == []


def test_voice_callback_without_sender_is_ignored():
    store = InMemoryOrderStore()
    app = create_app(
        agent=build_agent(model=VoiceReadingLlm(), store=store),
        session_service=InMemorySessionService(),
        twilio_auth_token=AUTH_TOKEN,
    )
    client = TestClient(app)

    form = _callback()
    del form["From"]
    response = client.post(
        CALLBACK_URL, data=form, headers={"X-Twilio-Signature": _sign(form)}
    )
    assert response.status_code == 200
    assert store.orders == []
