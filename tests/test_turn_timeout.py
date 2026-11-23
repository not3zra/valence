"""Agent turns are cancellable and cannot stall the web event loop (#76)."""

from __future__ import annotations

import asyncio

import httpx
from fastapi.testclient import TestClient

from src.auth import AuthService, InMemoryAuthStore
from src.intake import IntakeService
from src.store import InMemoryOrderStore
from src.web import create_app

from .fakes import FakeRuntime

ROUTE = "/api/roundtrip"
HEADERS = {"Authorization": "Bearer probe-token"}
PAYLOAD = {"sender_id": "+919812345001", "message": "two drums of acid"}


class _HangingRuntime(FakeRuntime):
    def __init__(self) -> None:
        super().__init__(reply="done")
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run_turn_async(self, *, sender_id, message, media=None) -> str:
        self.started.set()
        await self.release.wait()
        return self.reply


def _app(runtime, *, timeout: float = 1.0) -> object:
    return create_app(
        runtime=runtime,
        roundtrip_token="probe-token",
        agent_turn_timeout_s=timeout,
    )


async def test_slow_turn_does_not_block_health_check():
    runtime = _HangingRuntime()
    transport = httpx.ASGITransport(app=_app(runtime))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        turn = asyncio.create_task(
            client.post(ROUTE, json=PAYLOAD, headers=HEADERS)
        )
        await asyncio.wait_for(runtime.started.wait(), timeout=1)
        health = await asyncio.wait_for(client.get("/health"), timeout=1)
        assert health.status_code == 200
        runtime.release.set()
        response = await asyncio.wait_for(turn, timeout=1)
    assert response.status_code == 200
    assert response.json()["reply"] == "done"


async def test_hung_turn_returns_504_within_the_timeout():
    runtime = _HangingRuntime()
    transport = httpx.ASGITransport(app=_app(runtime, timeout=0.02))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await asyncio.wait_for(
            client.post(ROUTE, json=PAYLOAD, headers=HEADERS), timeout=1
        )
    assert response.status_code == 504


async def test_rest_intake_turn_timeout_returns_504():
    runtime = _HangingRuntime()
    store = InMemoryOrderStore()
    intake = IntakeService(runtime, store, turn_timeout_s=0.02)
    auth = AuthService(InMemoryAuthStore(), setup_token="setup-secret")
    app = create_app(auth=auth, store=store, intake_service=intake)
    client = TestClient(app)
    created = client.post(
        "/api/v1/setup",
        json={"setup_token": "setup-secret", "business_name": "Acme"},
    )
    assert created.status_code == 201
    headers = {"Authorization": f"Bearer {created.json()['token']}"}
    response = client.post(
        "/api/v1/intake",
        json={"sender_id": "+919812345001", "message": "two drums of acid"},
        headers=headers,
    )
    assert response.status_code == 504
