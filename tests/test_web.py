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


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_is_reserved_by_cloud_run(client):
    # Paths ending in "z" (/healthz) never reach the container on Cloud Run.
    response = client.get("/healthz")
    assert response.status_code == 404


def test_retired_twilio_voice_callback_route_is_removed(client):
    # The Twilio Voice recording-status callback was retired in #37; the only
    # voice path is the v1 intake route, which carries an Agent Interface key.
    response = client.post(
        "/api/voice/callback",
        data={"RecordingStatus": "completed", "From": "+919812345001"},
    )
    assert response.status_code == 404
