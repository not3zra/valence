"""The token-gated company-recorded call ingestion endpoint (issue #35).

The company's own system posts a recorded call (audio bytes + the caller's
E.164 number) with a per-deploy bearer token; the endpoint runs it through the
same ADK agent turn as the other channels and commits a voice order. A voice
order with a missing field escalates as flagged — it never waits on a
clarifying question (ADR-0004). The token is verified before anything is
parsed; the body is size-capped before it is buffered; the caller is a
whole-string E.164 number.
"""

from __future__ import annotations

import base64

from fastapi.testclient import TestClient
from google.adk.sessions import InMemorySessionService

from src.agent import build_agent
from src.config import settings
from src.media import MAX_MEDIA_BYTES
from src.store import InMemoryOrderStore
from src.web import MAX_VOICE_INGEST_BODY_BYTES, create_app

from .fakes import VoiceMissingFieldLlm, VoiceReadingLlm

INGEST_URL = "/api/voice/ingest"
TOKEN = "ingest-token"
AUDIO = b"RIFF fake-wav"


def _client(
    *, store=None, model=None, token=TOKEN
) -> TestClient:
    app = create_app(
        agent=build_agent(model=model or VoiceReadingLlm(), store=store),
        session_service=InMemorySessionService(),
        voice_ingest_token=token,
    )
    return TestClient(app)


def _payload(
    caller: str = "+919812345001",
    audio: bytes = AUDIO,
    mime: str = "audio/wav",
) -> dict:
    return {
        "caller": caller,
        "audio_base64": base64.b64encode(audio).decode(),
        "mime_type": mime,
    }


def _auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_ingest_commits_a_voice_order():
    store = InMemoryOrderStore()
    client = _client(store=store)
    response = client.post(INGEST_URL, json=_payload(), headers=_auth())
    assert response.status_code == 200

    assert len(store.orders) == 1
    order = store.orders[0]
    assert order.phone == "+919812345001"
    assert order.source_channel == "voice"
    assert order.status.value == "approved"


def test_ingest_missing_field_escalates_never_clarifies():
    store = InMemoryOrderStore()
    client = _client(store=store, model=VoiceMissingFieldLlm())
    response = client.post(INGEST_URL, json=_payload(), headers=_auth())
    assert response.status_code == 200

    # The voice order is escalated as flagged — never held for a clarifying
    # question (ADR-0004), and never silently approved.
    assert len(store.orders) == 1
    order = store.orders[0]
    assert order.status.value == "pending_review"
    assert "missing_field" in order.escalation_reasons


def test_ingest_rejects_a_missing_token():
    store = InMemoryOrderStore()
    client = _client(store=store)
    response = client.post(INGEST_URL, json=_payload())
    assert response.status_code == 401
    assert store.orders == []


def test_ingest_rejects_a_wrong_token():
    store = InMemoryOrderStore()
    client = _client(store=store)
    response = client.post(
        INGEST_URL, json=_payload(), headers={"Authorization": "Bearer wrong"}
    )
    assert response.status_code == 401
    assert store.orders == []


def test_ingest_is_closed_when_token_unconfigured(monkeypatch):
    # With no token configured the endpoint is closed (503), never open — the
    # caller identity is trusted company metadata, so the bearer is the only
    # way in. ``token=None`` falls back to the environment, so the env and the
    # settings object are pinned to "unconfigured" to make the closedness real
    # regardless of the ambient shell.
    monkeypatch.delenv("VOICE_INGEST_TOKEN", raising=False)
    monkeypatch.setattr(settings, "voice_ingest_token", "")
    client = _client(token=None)
    response = client.post(INGEST_URL, json=_payload(), headers=_auth())
    assert response.status_code == 503


def test_ingest_rejects_before_parsing_when_token_is_invalid():
    # The token check precedes every parse: a malformed/oversized body with a
    # wrong token is a 401, not a 400/413 — nothing is read or parsed first.
    store = InMemoryOrderStore()
    client = _client(store=store)
    response = client.post(
        INGEST_URL,
        content="x" * (MAX_VOICE_INGEST_BODY_BYTES + 1),
        headers={"content-type": "application/json", "Authorization": "Bearer nope"},
    )
    assert response.status_code == 401
    assert store.orders == []


def test_ingest_rejects_a_non_e164_caller():
    store = InMemoryOrderStore()
    client = _client(store=store)
    for caller in ["not-a-phone", "+919812345001 ", "919812345001", "+0"]:
        response = client.post(
            INGEST_URL, json=_payload(caller=caller), headers=_auth()
        )
        assert response.status_code == 400
    assert store.orders == []


def test_ingest_rejects_missing_or_non_string_caller():
    client = _client()
    for caller in [None, 5, ""]:
        payload = _payload()
        payload["caller"] = caller
        response = client.post(INGEST_URL, json=payload, headers=_auth())
        assert response.status_code == 400


def test_ingest_rejects_non_base64_audio():
    store = InMemoryOrderStore()
    client = _client(store=store)
    payload = _payload()
    payload["audio_base64"] = "not!base64!!"
    response = client.post(INGEST_URL, json=payload, headers=_auth())
    assert response.status_code == 400
    assert store.orders == []


def test_ingest_rejects_empty_audio():
    client = _client()
    payload = _payload()
    payload["audio_base64"] = ""
    response = client.post(INGEST_URL, json=payload, headers=_auth())
    assert response.status_code == 400


def test_ingest_rejects_audio_over_the_size_cap():
    store = InMemoryOrderStore()
    client = _client(store=store)
    payload = _payload(audio=b"x" * (MAX_MEDIA_BYTES + 1))
    response = client.post(INGEST_URL, json=payload, headers=_auth())
    assert response.status_code == 413
    assert store.orders == []


def test_ingest_rejects_an_oversized_body_before_buffering():
    store = InMemoryOrderStore()
    client = _client(store=store)
    response = client.post(
        INGEST_URL,
        content="x" * (MAX_VOICE_INGEST_BODY_BYTES + 1),
        headers={"content-type": "application/json", **_auth()},
    )
    assert response.status_code == 413
    assert store.orders == []


def test_ingest_rejects_malformed_json():
    client = _client()
    response = client.post(
        INGEST_URL,
        content="not json",
        headers={"content-type": "application/json", **_auth()},
    )
    assert response.status_code == 400


def test_ingest_rejects_a_non_dict_payload():
    client = _client()
    response = client.post(INGEST_URL, json=["not", "a", "dict"], headers=_auth())
    assert response.status_code == 400


def test_ingest_rejects_an_unknown_mime_type():
    store = InMemoryOrderStore()
    client = _client(store=store)
    payload = _payload(mime="text/html")
    response = client.post(INGEST_URL, json=payload, headers=_auth())
    assert response.status_code == 400
    assert store.orders == []


def test_ingest_defaults_mime_to_audio_wav():
    store = InMemoryOrderStore()
    client = _client(store=store)
    payload = _payload()
    del payload["mime_type"]
    response = client.post(INGEST_URL, json=payload, headers=_auth())
    assert response.status_code == 200
    assert store.orders[0].source_channel == "voice"


def test_ingest_accepts_either_mp3_mime_convention():
    # Gemini accepts both audio/mpeg and audio/mp3 for MP3; a feed may use
    # either.
    for mime in ["audio/mpeg", "audio/mp3"]:
        store = InMemoryOrderStore()
        client = _client(store=store)
        response = client.post(
            INGEST_URL, json=_payload(mime=mime), headers=_auth()
        )
        assert response.status_code == 200
