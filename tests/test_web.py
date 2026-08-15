"""HTTP surface of the web layer, exercised end-to-end against a fake LLM."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from google.adk.sessions import InMemorySessionService

from src.agent import build_agent
from src.web import create_app

from .fakes import FakeEchoLlm


@pytest.fixture
def client():
    app = create_app(
        agent=build_agent(model=FakeEchoLlm()),
        session_service=InMemorySessionService(),
        roundtrip_token="probe-token",
    )
    return TestClient(app)


def test_index_page_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Valence" in response.text


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_is_reserved_by_cloud_run(client):
    # Paths ending in "z" (/healthz) never reach the container on Cloud Run.
    response = client.get("/healthz")
    assert response.status_code == 404


def test_roundtrip_returns_reply(client):
    response = client.post(
        "/api/roundtrip",
        json={"sender_id": "+919812345001", "message": "two drums of acid"},
        headers={"Authorization": "Bearer probe-token"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["sender_id"] == "+919812345001"
    assert body["reply"] == "Echo: two drums of acid"


def test_roundtrip_requires_bearer_token():
    # The roundtrip probe lets the caller supply the sender id, so it must be
    # gated by a dedicated bearer token that is always required — otherwise an
    # unauthenticated caller could impersonate any phone, including an
    # allowlisted approver (issue #7, security #28).
    app = create_app(
        agent=build_agent(model=FakeEchoLlm()),
        session_service=InMemorySessionService(),
        roundtrip_token="probe-token",
    )
    client = TestClient(app)

    response = client.post(
        "/api/roundtrip",
        json={"sender_id": "+919812345001", "message": "hi"},
    )
    assert response.status_code == 401

    response = client.post(
        "/api/roundtrip",
        json={"sender_id": "+919812345001", "message": "hi"},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401

    response = client.post(
        "/api/roundtrip",
        json={"sender_id": "+919812345001", "message": "hi"},
        headers={"Authorization": "Bearer probe-token"},
    )
    assert response.status_code == 200


def test_roundtrip_disabled_when_token_unconfigured():
    # With no token configured the probe is closed (503), never open — the
    # finding that drove security #28.
    app = create_app(
        agent=build_agent(model=FakeEchoLlm()),
        session_service=InMemorySessionService(),
        roundtrip_token=None,
    )
    client = TestClient(app)
    response = client.post(
        "/api/roundtrip",
        json={"sender_id": "+919812345001", "message": "hi"},
        headers={"Authorization": "Bearer probe-token"},
    )
    assert response.status_code == 503


def test_roundtrip_rejects_missing_fields(client):
    response = client.post("/api/roundtrip", json={"message": "no sender"})
    assert response.status_code == 422


def test_roundtrip_rejects_non_e164_sender_id(client):
    response = client.post(
        "/api/roundtrip", json={"sender_id": "not-a-phone", "message": "hi"}
    )
    assert response.status_code == 422


def test_roundtrip_rejects_empty_message(client):
    response = client.post(
        "/api/roundtrip", json={"sender_id": "+919812345001", "message": ""}
    )
    assert response.status_code == 422


def test_sessions_survive_across_roundtrip_requests(client):
    client.post(
        "/api/roundtrip",
        json={"sender_id": "+919812345001", "message": "one"},
        headers={"Authorization": "Bearer probe-token"},
    )
    response = client.post(
        "/api/roundtrip",
        json={"sender_id": "+919812345001", "message": "two"},
        headers={"Authorization": "Bearer probe-token"},
    )
    assert response.json()["reply"] == "Echo: two"
