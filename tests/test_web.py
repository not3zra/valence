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
    )
    return TestClient(app)


def test_index_page_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Valence" in response.text


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_roundtrip_returns_reply(client):
    response = client.post(
        "/api/roundtrip",
        json={"sender_id": "+919812345001", "message": "two drums of acid"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["sender_id"] == "+919812345001"
    assert body["reply"] == "Echo: two drums of acid"


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
        "/api/roundtrip", json={"sender_id": "+919812345001", "message": "one"}
    )
    response = client.post(
        "/api/roundtrip", json={"sender_id": "+919812345001", "message": "two"}
    )
    assert response.json()["reply"] == "Echo: two"
