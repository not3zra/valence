"""REST intake bodies are size-capped before Pydantic parses them (#90)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api_v1 import MAX_MEDIA_INTAKE_BODY_BYTES, MAX_TEXT_INTAKE_BODY_BYTES
from src.auth import AuthService, InMemoryAuthStore
from src.store import InMemoryOrderStore
from src.web import create_app

from .fakes import FakeRuntime

SETUP = {"setup_token": "setup-secret", "business_name": "Acme"}


def _client() -> tuple[TestClient, dict[str, str]]:
    store = InMemoryOrderStore()
    auth = AuthService(InMemoryAuthStore(), setup_token="setup-secret")
    app = create_app(runtime=FakeRuntime(store=store), auth=auth, store=store)
    client = TestClient(app)
    created = client.post("/api/v1/setup", json=SETUP)
    assert created.status_code == 201
    return client, {"Authorization": f"Bearer {created.json()['token']}"}


@pytest.mark.parametrize(
    ("path", "field", "cap"),
    [
        ("/api/v1/intake", "message", MAX_TEXT_INTAKE_BODY_BYTES),
        ("/api/v1/intake/voice", "audio_base64", MAX_MEDIA_INTAKE_BODY_BYTES),
        ("/api/v1/intake/photo", "image_base64", MAX_MEDIA_INTAKE_BODY_BYTES),
    ],
)
def test_oversized_intake_body_is_rejected(path, field, cap):
    client, headers = _client()
    payload = {field: "x" * (cap + 1)}
    response = client.post(path, json=payload, headers=headers)
    assert response.status_code == 413


def test_auth_is_checked_before_the_body_is_read():
    client, _ = _client()
    response = client.post(
        "/api/v1/intake/voice",
        content=b"x" * (MAX_MEDIA_INTAKE_BODY_BYTES + 1),
        headers={
            "content-type": "application/json",
            "Authorization": "Bearer nope",
        },
    )
    assert response.status_code == 401


def test_small_text_intake_still_succeeds():
    client, headers = _client()
    response = client.post(
        "/api/v1/intake",
        json={"sender_id": "+919812345001", "message": "two drums of acid"},
        headers=headers,
    )
    assert response.status_code == 200


def test_invalid_sender_still_returns_422():
    client, headers = _client()
    response = client.post(
        "/api/v1/intake",
        json={"sender_id": "not-a-phone", "message": "two drums of acid"},
        headers=headers,
    )
    assert response.status_code == 422
