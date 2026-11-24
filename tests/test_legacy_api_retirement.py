"""Legacy non-v1 API retirement (issue #99).

The five pre-v1 routes are documented, non-functional paths: they return 404,
do not appear in OpenAPI, and every capability they offered is available through
an authenticated Agent Interface route. This module pins both halves of that
contract so a legacy route cannot quietly come back.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.auth import AuthService, InMemoryAuthStore, Role
from src.store import InMemoryOrderStore
from src.web import create_app

from .fakes import FakeRuntime

LEGACY_ROUTES = [
    ("POST", "/api/roundtrip"),
    ("GET", "/api/whatsapp/webhook"),
    ("POST", "/api/whatsapp/webhook"),
    ("POST", "/api/voice/ingest"),
    ("GET", "/api/vouchers"),
    ("POST", "/api/cutoff"),
]

V1_REPLACEMENTS = [
    "/api/v1/intake",
    "/api/v1/intake/voice",
    "/api/v1/webhooks/whatsapp",
    "/api/v1/orders",
    "/api/v1/orders/{order_id}/voucher",
    "/api/v1/loading-lists",
]

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client_and_key():
    store = InMemoryOrderStore()
    auth = AuthService(InMemoryAuthStore(), setup_token="setup-secret")
    operator = asyncio.run(auth.issue_key(name="operator", role=Role.OPERATOR)).token
    app = create_app(runtime=FakeRuntime(store=store), auth=auth, store=store)
    return TestClient(app), operator


@pytest.mark.parametrize(("method", "path"), LEGACY_ROUTES)
def test_legacy_route_returns_404(client_and_key, method, path):
    client, _ = client_and_key
    response = client.request(method, path, json={})
    assert response.status_code == 404


def test_legacy_routes_are_absent_from_openapi(client_and_key):
    client, _ = client_and_key
    paths = client.get("/openapi.json").json()["paths"]
    for _, path in LEGACY_ROUTES:
        assert path not in paths


def test_v1_replacements_are_present_in_openapi(client_and_key):
    client, _ = client_and_key
    paths = client.get("/openapi.json").json()["paths"]
    for path in V1_REPLACEMENTS:
        assert path in paths


def test_legacy_api_md_maps_every_retired_path_to_its_replacement():
    document = (ROOT / "LEGACY_API.md").read_text(encoding="utf-8")
    for _, path in LEGACY_ROUTES:
        assert path in document
    for path in V1_REPLACEMENTS:
        assert path in document


def test_v1_intake_replaces_the_roundtrip_probe(client_and_key):
    client, operator = client_and_key
    response = client.post(
        "/api/v1/intake",
        json={"sender_id": "+919812345001", "message": "two drums of acid"},
        headers={"Authorization": f"Bearer {operator}"},
    )
    assert response.status_code == 200
    assert response.json()["reply"] == "fake reply"


def test_v1_voice_intake_replaces_the_voice_ingest_route(client_and_key):
    client, operator = client_and_key
    response = client.post(
        "/api/v1/intake/voice",
        json={
            "caller": "+919812345001",
            "audio_base64": base64.b64encode(b"RIFF fake-wav").decode(),
            "mime_type": "audio/wav",
        },
        headers={"Authorization": f"Bearer {operator}"},
    )
    assert response.status_code == 200
    assert response.json()["reply"] == "fake reply"


def test_v1_orders_replaces_the_vouchers_listing(client_and_key):
    client, operator = client_and_key
    response = client.get(
        "/api/v1/orders", headers={"Authorization": f"Bearer {operator}"}
    )
    assert response.status_code == 200
    assert response.json() == []


def test_v1_loading_lists_replaces_the_cutoff_route(client_and_key):
    client, operator = client_and_key
    response = client.get(
        "/api/v1/loading-lists", headers={"Authorization": f"Bearer {operator}"}
    )
    assert response.status_code == 200
    assert "delivery_day" in response.json()
