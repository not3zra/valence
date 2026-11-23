"""Idempotent order actions over REST, MCP and the durable store (issue #95).

The seams are the ones the issue pre-agrees: the shared ``OrderActionService``
both transports call, the v1 REST action route with its Idempotency-Key header,
the MCP ``order_action`` tool, and the SQL idempotency store the intake path
already made durable.
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
from src.core import OrderProcessingCore
from src.intake import IdempotencyConflict, InMemoryIdempotencyStore
from src.mcp_auth import McpToolGateway, build_order_tool_operations
from src.order_actions import OrderActionService
from src.orders import Order, OrderItem, OrderStatus
from src.sql_store import SqlIdempotencyStore, SqlOrderStore
from src.store import InMemoryOrderStore
from src.tally import TallyPushFailed, TallyPushResult
from src.voucher import InMemoryVoucherStore
from src.web import create_app

from .fakes import FakeRuntime, approved_order_id

APPROVER_PHONE = "+919845000001"
CUSTOMER_PHONE = "+919812345001"
POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL", "")


def _sqlite_url(tmp_path, name="actions.db"):
    return f"sqlite+aiosqlite:///{tmp_path / name}"


@pytest.fixture(params=["sqlite", "postgres"])
def store_url(request, tmp_path):
    if request.param == "postgres":
        if not POSTGRES_URL:
            pytest.skip("set TEST_POSTGRES_URL to run Postgres conformance")
        return POSTGRES_URL
    return _sqlite_url(tmp_path)


async def _approver_phones():
    return [APPROVER_PHONE]


def _auth():
    return AuthService(InMemoryAuthStore(), approver_phones=_approver_phones)


async def _principal():
    auth = _auth()
    key = await auth.issue_key(
        name="approver", role=Role.APPROVER, phone=APPROVER_PHONE
    )
    principal = await auth.authenticate(key.token)
    assert principal is not None
    return principal


def _service(store, *, voucher_storage=None, tally_push=None, idempotency=None):
    return OrderActionService(
        OrderProcessingCore(store),
        store,
        voucher_storage,
        tally_push,
        idempotency=idempotency,
    )


async def _seed(store, order_id, *, status=OrderStatus.PENDING_REVIEW, **fields):
    order = Order(
        order_id=order_id,
        phone=CUSTOMER_PHONE,
        items=[OrderItem(product="sulfuric acid", quantity=2000, unit="kg")],
        status=status,
        **fields,
    )
    await store.create_order(order)
    return order


async def _events(store, order_id) -> list[str]:
    return [event.event_type for event in await store.list_order_events(order_id)]


class _CountingPush:
    """Tally boundary double that counts submissions instead of using network."""

    def __init__(self, *, fail_times: int = 0) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_times = fail_times

    async def push(self, order_id, *, by=""):
        self.calls.append((order_id, by))
        if self.fail_times:
            self.fail_times -= 1
            return TallyPushResult(ok=False, error="tally is down")
        return TallyPushResult(ok=True)


async def test_replaying_an_approval_returns_the_original_result_and_audits_once():
    store = InMemoryOrderStore()
    await _seed(store, "ord_replay")
    service = _service(store)
    principal = await _principal()
    arguments = {
        "order_id": "ord_replay",
        "action": "approve",
        "idempotency_key": "k-1",
    }

    first = await service.act(arguments, principal)
    replay = await service.act(arguments, principal)

    assert first["idempotent"] is False
    assert replay["idempotent"] is True
    assert first["status"] == replay["status"] == "approved"
    assert (await _events(store, "ord_replay")).count("order_approved") == 1


async def test_replaying_prepare_voucher_returns_the_same_voucher_without_a_duplicate():
    store = InMemoryOrderStore()
    storage = InMemoryVoucherStore()
    order_id = await approved_order_id(store)
    service = _service(store, voucher_storage=storage)
    principal = await _principal()
    arguments = {
        "order_id": order_id,
        "action": "prepare_voucher",
        "idempotency_key": "voucher-key",
    }

    first = await service.act(arguments, principal)
    replay = await service.act(arguments, principal)

    assert first["idempotent"] is False
    assert replay["idempotent"] is True
    assert (
        first["order"]["voucher_id"]
        == replay["order"]["voucher_id"]
        == f"voucher_{order_id}"
    )
    assert (await _events(store, order_id)).count("voucher_ready") == 1
    assert list(storage.blobs) == [f"voucher_{order_id}"]


async def test_replaying_push_to_tally_does_not_push_twice():
    store = InMemoryOrderStore()
    await _seed(
        store, "ord_push", status=OrderStatus.APPROVED, voucher_id="voucher_ord_push"
    )
    push = _CountingPush()
    service = _service(store, tally_push=push)
    principal = await _principal()
    arguments = {
        "order_id": "ord_push",
        "action": "push_to_tally",
        "idempotency_key": "push-key",
    }

    first = await service.act(arguments, principal)
    replay = await service.act(arguments, principal)

    assert first["idempotent"] is False
    assert replay["idempotent"] is True
    assert push.calls == [("ord_push", principal.id)]


async def test_reusing_an_action_key_with_a_different_payload_conflicts():
    store = InMemoryOrderStore()
    await _seed(store, "ord_conflict")
    service = _service(store)
    principal = await _principal()

    await service.act(
        {"order_id": "ord_conflict", "action": "approve", "idempotency_key": "k-2"},
        principal,
    )
    with pytest.raises(IdempotencyConflict):
        await service.act(
            {"order_id": "ord_conflict", "action": "reject", "idempotency_key": "k-2"},
            principal,
        )

    assert (await _events(store, "ord_conflict")).count("order_approved") == 1
    assert "order_rejected" not in await _events(store, "ord_conflict")


async def test_a_failed_action_releases_its_key_for_a_retry():
    store = InMemoryOrderStore()
    await _seed(
        store, "ord_retry", status=OrderStatus.APPROVED, voucher_id="voucher_ord_retry"
    )
    push = _CountingPush(fail_times=1)
    service = _service(store, tally_push=push)
    principal = await _principal()
    arguments = {
        "order_id": "ord_retry",
        "action": "push_to_tally",
        "idempotency_key": "retry-key",
    }

    with pytest.raises(TallyPushFailed):
        await service.act(arguments, principal)
    replay = await service.act(arguments, principal)

    assert len(push.calls) == 2
    assert replay["idempotent"] is False


async def test_concurrent_approvals_with_one_key_audit_once():
    store = InMemoryOrderStore()
    await _seed(store, "ord_concurrent")
    idempotency = InMemoryIdempotencyStore()
    principal = await _principal()
    workers = [_service(store, idempotency=idempotency) for _ in range(2)]
    arguments = {
        "order_id": "ord_concurrent",
        "action": "approve",
        "idempotency_key": "concurrent-key",
    }

    first, second = await asyncio.gather(
        *(worker.act(arguments, principal) for worker in workers)
    )

    assert {first["idempotent"], second["idempotent"]} == {False, True}
    assert (await _events(store, "ord_concurrent")).count("order_approved") == 1


def test_rest_action_replays_with_a_header_and_conflicts_on_a_new_payload():
    store = InMemoryOrderStore()
    asyncio.run(_seed(store, "ord_rest"))
    runtime = FakeRuntime(store=store)
    auth = _auth()
    approver = asyncio.run(
        auth.issue_key(name="approver", role=Role.APPROVER, phone=APPROVER_PHONE)
    )
    client = TestClient(create_app(runtime=runtime, auth=auth))
    url = "/api/v1/orders/ord_rest/actions"
    headers = {
        "Authorization": f"Bearer {approver.token}",
        "Idempotency-Key": "rest-key",
    }

    first = client.post(
        url, json={"action": "approve", "confirm": True}, headers=headers
    )
    replay = client.post(
        url, json={"action": "approve", "confirm": True}, headers=headers
    )
    conflict = client.post(
        url, json={"action": "reject", "confirm": True}, headers=headers
    )

    assert first.status_code == replay.status_code == 200
    assert first.json()["idempotent"] is False
    assert replay.json()["idempotent"] is True
    assert first.json()["status"] == replay.json()["status"] == "approved"
    assert conflict.status_code == 409
    assert (asyncio.run(_events(store, "ord_rest"))).count("order_approved") == 1


async def test_mcp_order_action_honours_an_idempotency_key():
    store = InMemoryOrderStore()
    await _seed(store, "ord_mcp")
    auth = _auth()
    approver = await auth.issue_key(
        name="approver", role=Role.APPROVER, phone=APPROVER_PHONE
    )
    service = _service(store)
    gateway = McpToolGateway(auth, build_order_tool_operations(service))
    arguments = {
        "order_id": "ord_mcp",
        "action": "approve",
        "confirm": True,
        "idempotency_key": "mcp-key",
    }

    first = await gateway.call(
        "order_action", api_key=approver.token, arguments=arguments
    )
    replay = await gateway.call(
        "order_action", api_key=approver.token, arguments=arguments
    )

    assert first["idempotent"] is False
    assert replay["idempotent"] is True
    assert (await _events(store, "ord_mcp")).count("order_approved") == 1


async def test_mcp_reusing_an_action_key_with_a_different_payload_conflicts():
    store = InMemoryOrderStore()
    await _seed(store, "ord_mcp_conflict")
    auth = _auth()
    approver = await auth.issue_key(
        name="approver", role=Role.APPROVER, phone=APPROVER_PHONE
    )
    gateway = McpToolGateway(auth, build_order_tool_operations(_service(store)))
    common = {
        "order_id": "ord_mcp_conflict",
        "confirm": True,
        "idempotency_key": "mcp-conflict-key",
    }

    await gateway.call(
        "order_action",
        api_key=approver.token,
        arguments={**common, "action": "approve"},
    )
    with pytest.raises(IdempotencyConflict):
        await gateway.call(
            "order_action",
            api_key=approver.token,
            arguments={**common, "action": "reject"},
        )

    assert (await _events(store, "ord_mcp_conflict")).count("order_approved") == 1
    assert "order_rejected" not in await _events(store, "ord_mcp_conflict")


async def test_replaying_an_approval_after_a_voucher_exists_keeps_one_voucher():
    store = InMemoryOrderStore()
    storage = InMemoryVoucherStore()
    service = _service(store, voucher_storage=storage)
    principal = await _principal()
    order = await _seed(
        store,
        "ord_approved_voucher",
        customer_id="c_chemfab",
        delivery_location_id="dl_peenya",
    )
    approve = {
        "order_id": order.order_id,
        "action": "approve",
        "idempotency_key": "approval-with-voucher",
    }

    first = await service.act(approve, principal)
    await service.act(
        {
            "order_id": order.order_id,
            "action": "prepare_voucher",
            "idempotency_key": "voucher-after-approval",
        },
        principal,
    )
    replay = await service.act(approve, principal)
    updated = await store.get_order(order.order_id)

    assert first["idempotent"] is False
    assert replay["idempotent"] is True
    assert replay["status"] == first["status"] == "approved"
    assert updated is not None and updated.voucher_id == "voucher_ord_approved_voucher"
    assert (await _events(store, order.order_id)).count("voucher_ready") == 1
    assert list(storage.blobs) == ["voucher_ord_approved_voucher"]


async def test_sql_action_result_replays_after_a_worker_restart(store_url):
    await SqlOrderStore(store_url).initialize()
    numbers = itertools.count(1)
    key = f"restart-{uuid.uuid4().hex}"

    def worker():
        store = SqlOrderStore(store_url)
        return _service(store, idempotency=SqlIdempotencyStore(store_url))

    principal = await _principal()
    first_store = SqlOrderStore(store_url)
    first_order = await _seed(first_store, f"ord_{next(numbers)}")
    first = await _service(
        first_store, idempotency=SqlIdempotencyStore(store_url)
    ).act(
        {"order_id": first_order.order_id, "action": "approve", "idempotency_key": key},
        principal,
    )
    restarted = worker()
    replay = await restarted.act(
        {"order_id": first_order.order_id, "action": "approve", "idempotency_key": key},
        principal,
    )

    assert first["idempotent"] is False
    assert replay["idempotent"] is True
    assert replay["status"] == first["status"] == "approved"
    assert (await _events(SqlOrderStore(store_url), first_order.order_id)).count(
        "order_approved"
    ) == 1


async def test_two_sql_workers_approving_once_create_one_decision(store_url):
    url = store_url
    await SqlOrderStore(url).initialize()
    order = await _seed(SqlOrderStore(url), f"ord_{uuid.uuid4().hex}")
    principal = await _principal()
    key = f"concurrent-{uuid.uuid4().hex}"
    workers = [
        _service(SqlOrderStore(url), idempotency=SqlIdempotencyStore(url))
        for _ in range(2)
    ]
    arguments = {
        "order_id": order.order_id,
        "action": "approve",
        "idempotency_key": key,
    }

    first, second = await asyncio.gather(
        *(worker.act(arguments, principal) for worker in workers)
    )

    assert {first["idempotent"], second["idempotent"]} == {False, True}
    assert (await _events(SqlOrderStore(url), order.order_id)).count(
        "order_approved"
    ) == 1


async def test_build_agent_services_wires_idempotent_actions_for_sql_orders(tmp_path):
    url = _sqlite_url(tmp_path)
    order = await _seed(SqlOrderStore(url), "ord_wired")
    await SqlOrderStore(url).initialize()
    principal = await _principal()
    key = f"wired-{uuid.uuid4().hex}"

    def services():
        store = SqlOrderStore(url)
        runtime = FakeRuntime(store=store)
        return build_agent_services(runtime, None, None).orders

    first = await services().act(
        {"order_id": order.order_id, "action": "approve", "idempotency_key": key},
        principal,
    )
    replay = await services().act(
        {"order_id": order.order_id, "action": "approve", "idempotency_key": key},
        principal,
    )

    assert first["idempotent"] is False
    assert replay["idempotent"] is True
    assert (await _events(SqlOrderStore(url), order.order_id)).count(
        "order_approved"
    ) == 1
