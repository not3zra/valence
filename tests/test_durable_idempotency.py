"""Durable, concurrency-safe intake idempotency (issue #94).

The seams are the ones the issue pre-agrees: the ``IdempotencyStore`` port, the
``IntakeService`` that claims keys before running a turn, the SQL adapter and its
Alembic migration on SQLite/Postgres, and the v1 REST transport.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from src.auth import AuthService, InMemoryAuthStore, Role
from src.composition import build_agent_services
from src.intake import (
    ClaimStatus,
    InMemoryIdempotencyStore,
    IntakeService,
)
from src.model_provider import ModelCapabilities
from src.orders import Order, OrderStatus
from src.sql_store import SqlIdempotencyStore, SqlOrderStore
from src.store import InMemoryOrderStore
from src.web import create_app

from .fakes import FakeRuntime

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL", "")


def _sqlite_url(tmp_path, name="idempotency.db"):
    return f"sqlite+aiosqlite:///{tmp_path / name}"


@pytest.fixture(params=["sqlite", "postgres"])
def store_url(request, tmp_path):
    if request.param == "postgres":
        if not POSTGRES_URL:
            pytest.skip("set TEST_POSTGRES_URL to run Postgres conformance")
        return POSTGRES_URL
    return _sqlite_url(tmp_path)


class _CommittingRunner:
    """Turn runner that commits one approved order into the shared store."""

    def __init__(self, store, numbers) -> None:
        self.store = store
        self.numbers = numbers
        self.calls = 0

    @property
    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities()

    def run_turn(self, *, sender_id, message, media=None) -> str:
        return "reply"

    async def run_turn_async(self, *, sender_id, message, media=None) -> str:
        self.calls += 1
        await asyncio.sleep(0.01)
        order = Order(
            phone=sender_id,
            items=[],
            order_id=f"ord_{next(self.numbers)}",
            status=OrderStatus.APPROVED,
        )
        await self.store.create_order(order)
        return "reply"


class _FailingRunner:
    def __init__(self, failures: int) -> None:
        self.failures = failures

    @property
    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities()

    def run_turn(self, *, sender_id, message, media=None) -> str:
        return "reply"

    async def run_turn_async(self, *, sender_id, message, media=None) -> str:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("turn failed")
        return "reply"


class _SqlRuntime(FakeRuntime):
    def __init__(self, store, numbers) -> None:
        super().__init__(store=store)
        self.runner = _CommittingRunner(store, numbers)

    def run_turn(self, *, sender_id, message, media=None) -> str:
        self.turns.append((sender_id, message, media))
        return self.runner.run_turn(sender_id=sender_id, message=message, media=media)

    async def run_turn_async(self, *, sender_id, message, media=None) -> str:
        return await self.runner.run_turn_async(
            sender_id=sender_id, message=message, media=media
        )


async def _order_count(store) -> int:
    return len(await store.list_all_orders())


async def test_two_intake_services_sharing_an_idempotency_store_create_one_order():
    orders = InMemoryOrderStore()
    idempotency = InMemoryIdempotencyStore()
    numbers = itertools.count(1)
    workers = [
        IntakeService(
            _CommittingRunner(orders, numbers), orders, idempotency=idempotency
        )
        for _ in range(2)
    ]
    kwargs = {
        "sender_id": "+919812345001",
        "message": "2000 kg sulfuric acid",
        "principal_id": "principal-1",
        "idempotency_key": "retry-1",
        "fingerprint": "fp-1",
    }

    first, second = await asyncio.gather(
        *(worker.intake_text(**kwargs) for worker in workers)
    )

    assert first.order_id == second.order_id
    assert {first.idempotent, second.idempotent} == {False, True}
    assert await _order_count(orders) == 1


async def test_a_failed_turn_releases_the_key_for_a_different_payload():
    orders = InMemoryOrderStore()
    service = IntakeService(_FailingRunner(failures=1), orders)
    kwargs = {
        "sender_id": "+919812345001",
        "message": "first message",
        "principal_id": "principal-1",
        "idempotency_key": "failed-retry",
        "fingerprint": "fp-1",
    }

    with pytest.raises(RuntimeError, match="turn failed"):
        await service.intake_text(**kwargs)

    replay = await service.intake_text(
        **{**kwargs, "message": "different message", "fingerprint": "fp-2"}
    )

    assert replay.idempotent is False


def test_sql_adapter_satisfies_the_same_port_as_the_double():
    port_methods = ("try_claim", "complete", "release", "cleanup_expired")
    assert all(hasattr(SqlIdempotencyStore, name) for name in port_methods)
    assert all(hasattr(InMemoryIdempotencyStore, name) for name in port_methods)


async def test_concurrent_claims_are_awarded_to_exactly_one_worker(store_url):
    first = SqlIdempotencyStore(store_url)
    second = SqlIdempotencyStore(store_url)
    await first.init_schema()
    await second.init_schema()
    key = f"key-{uuid.uuid4().hex}"

    claims = await asyncio.gather(
        first.try_claim("principal:text", key, "fp-1"),
        second.try_claim("principal:text", key, "fp-1"),
    )

    assert [claim.status for claim in claims].count(ClaimStatus.CLAIMED) == 1
    assert [claim.status for claim in claims].count(ClaimStatus.PENDING) == 1


async def test_completed_response_replays_after_a_restart(store_url):
    url = store_url
    key = f"key-{uuid.uuid4().hex}"
    result = {"reply": "first reply", "order_id": "ord_1", "idempotent": False}
    first = SqlIdempotencyStore(url)
    await first.init_schema()
    claim = await first.try_claim("principal:text", key, "fp-1")
    assert claim.status is ClaimStatus.CLAIMED
    await first.complete("principal:text", key, "fp-1", result)

    restarted = SqlIdempotencyStore(url)
    replay = await restarted.try_claim("principal:text", key, "fp-1")

    assert replay.status is ClaimStatus.COMPLETE
    assert replay.result == result


async def test_reusing_a_key_with_a_different_payload_reports_the_stored_fingerprint(
    store_url,
):
    store = SqlIdempotencyStore(store_url)
    await store.init_schema()
    key = f"key-{uuid.uuid4().hex}"
    await store.try_claim("principal:text", key, "fp-original")
    await store.complete("principal:text", key, "fp-original", {"reply": "one"})

    claim = await store.try_claim("principal:text", key, "fp-different")

    assert claim.status is ClaimStatus.COMPLETE
    assert claim.fingerprint == "fp-original"
    assert claim.result == {"reply": "one"}


async def test_releasing_a_pending_claim_allows_a_fresh_claim(store_url):
    store = SqlIdempotencyStore(store_url)
    await store.init_schema()
    key = f"key-{uuid.uuid4().hex}"
    await store.try_claim("principal:text", key, "fp-1")
    await store.release("principal:text", key)

    claim = await store.try_claim("principal:text", key, "fp-2")

    assert claim.status is ClaimStatus.CLAIMED
    assert claim.fingerprint == ""


async def test_cleanup_expired_removes_only_expired_records(tmp_path):
    live = SqlIdempotencyStore(_sqlite_url(tmp_path, "live.db"), ttl_s=3600)
    expiring = SqlIdempotencyStore(_sqlite_url(tmp_path, "expiring.db"), ttl_s=-1)
    await live.init_schema()
    await expiring.init_schema()
    await live.try_claim("principal:text", "live-key", "fp-live")
    await live.complete("principal:text", "live-key", "fp-live", {"reply": "live"})
    await expiring.try_claim("principal:text", "old-key", "fp-old")

    removed = await expiring.cleanup_expired()
    still_live = await live.cleanup_expired()

    assert removed == 1
    assert still_live == 0
    assert (
        await expiring.try_claim("principal:text", "old-key", "fp-old")
    ).status is ClaimStatus.CLAIMED
    assert (
        await live.try_claim("principal:text", "live-key", "fp-live")
    ).status is ClaimStatus.COMPLETE


async def test_a_new_claim_reclaims_expired_rows_without_a_same_key_retry(store_url):
    store = SqlIdempotencyStore(store_url, ttl_s=-1, cleanup_interval_s=0)
    await store.init_schema()
    await store.try_claim("principal:text", "first-unique-key", "fp-first")
    await store.complete(
        "principal:text", "first-unique-key", "fp-first", {"reply": "first"}
    )

    await store.try_claim("principal:text", "second-unique-key", "fp-second")

    assert await store._row("principal:text", "first-unique-key") is None


async def test_build_agent_services_wires_durable_idempotency_for_sql_orders(tmp_path):
    url = _sqlite_url(tmp_path)
    numbers = itertools.count(1)
    first = build_agent_services(_SqlRuntime(SqlOrderStore(url), numbers), None, None)
    second = build_agent_services(_SqlRuntime(SqlOrderStore(url), numbers), None, None)
    kwargs = {
        "sender_id": "+919812345001",
        "message": "2000 kg sulfuric acid",
        "principal_id": "principal-1",
        "idempotency_key": "wired-retry",
        "fingerprint": "fp-1",
    }

    one = await first.intake.intake_text(**kwargs)
    two = await second.intake.intake_text(**kwargs)

    assert one.order_id == two.order_id
    assert {one.idempotent, two.idempotent} == {False, True}
    assert len(await SqlOrderStore(url).list_all_orders()) == 1


async def test_two_sql_workers_create_one_order_for_the_same_key(store_url):
    await SqlOrderStore(store_url).initialize()
    numbers = itertools.count(1)
    workers = []
    for _ in range(2):
        orders = SqlOrderStore(store_url)
        idempotency = SqlIdempotencyStore(store_url)
        workers.append(
            IntakeService(
                _SqlRuntime(orders, numbers), orders, idempotency=idempotency
            )
        )
    kwargs = {
        "sender_id": "+919812345001",
        "message": "2000 kg sulfuric acid",
        "principal_id": "principal-1",
        "idempotency_key": f"worker-{uuid.uuid4().hex}",
        "fingerprint": "fp-1",
    }

    first, second = await asyncio.gather(
        *(worker.intake_text(**kwargs) for worker in workers)
    )

    assert first.order_id == second.order_id
    assert {first.idempotent, second.idempotent} == {False, True}
    assert await _order_count(SqlOrderStore(store_url)) == 1


async def test_completed_intake_replays_after_a_worker_restart(store_url):
    url = store_url
    key = f"restart-{uuid.uuid4().hex}"
    numbers = itertools.count(1)
    orders = SqlOrderStore(url)
    first = IntakeService(
        _SqlRuntime(orders, numbers),
        orders,
        idempotency=SqlIdempotencyStore(url),
    )
    original = await first.intake_text(
        sender_id="+919812345001",
        message="2000 kg sulfuric acid",
        principal_id="principal-1",
        idempotency_key=key,
        fingerprint="fp-1",
    )

    restarted_orders = SqlOrderStore(url)
    restarted = IntakeService(
        _SqlRuntime(restarted_orders, numbers),
        restarted_orders,
        idempotency=SqlIdempotencyStore(url),
    )
    replay = await restarted.intake_text(
        sender_id="+919812345001",
        message="2000 kg sulfuric acid",
        principal_id="principal-1",
        idempotency_key=key,
        fingerprint="fp-1",
    )

    assert replay.order_id == original.order_id
    assert original.idempotent is False
    assert replay.idempotent is True
    assert await _order_count(restarted_orders) == 1


def test_rest_intake_replays_across_app_restarts_with_a_sql_store(tmp_path):
    url = _sqlite_url(tmp_path)
    numbers = itertools.count(1)
    auth = AuthService(InMemoryAuthStore())
    operator = asyncio.run(auth.issue_key(name="operator", role=Role.OPERATOR)).token
    body = {"sender_id": "+919812345001", "message": "2000 kg sulfuric acid"}
    headers = {"Authorization": f"Bearer {operator}", "Idempotency-Key": "rest-retry"}

    def client():
        orders = SqlOrderStore(url)
        runtime = _SqlRuntime(orders, numbers)
        return TestClient(create_app(runtime=runtime, auth=auth)), orders

    first_client, first_orders = client()
    first = first_client.post("/api/v1/intake", json=body, headers=headers)
    second_client, second_orders = client()
    second = second_client.post("/api/v1/intake", json=body, headers=headers)

    assert first.status_code == second.status_code == 200
    assert first.json()["order_id"] == second.json()["order_id"]
    assert first.json()["idempotent"] is False
    assert second.json()["idempotent"] is True
    assert len(asyncio.run(second_orders.list_all_orders())) == 1
