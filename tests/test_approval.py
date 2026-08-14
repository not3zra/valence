"""The human approval path over WhatsApp (issue #7).

An escalated order is answered by an allowlisted approver with a pure yes/no:
``approved=True`` transitions the order to ``approved``, ``approved=False`` to
the terminal ``rejected`` status, each recorded as an Order Event. Only an
allowlisted approver may decide, only an order still sitting in
``pending_review`` may move, and a decision resolves to exactly one pending
order per approver (the pending-approval registry), so a non-allowlisted
number's reply — or an approver with nothing pending — can never act.
"""

from __future__ import annotations

import pytest

from src.core import ApprovalError, OrderProcessingCore
from src.orders import (
    EVENT_ORDER_APPROVED,
    EVENT_ORDER_REJECTED,
    Order,
    OrderItem,
    OrderStatus,
)
from src.store import InMemoryOrderStore

APPROVER_PHONE = "+919845000001"  # a_nikhil
NON_APPROVER_PHONE = "+919812345001"  # a real customer, not an approver


@pytest.fixture
def store():
    return InMemoryOrderStore()


@pytest.fixture
def core(store):
    return OrderProcessingCore(store)


def _order(**overrides) -> Order:
    defaults: dict = dict(
        phone="+919812345001",
        customer="ChemFab Industries",
        delivery_location="Peenya Industrial Area",
        confidence=0.9,
        source_channel="whatsapp",
        items=[OrderItem(product="sulfuric acid", quantity=2000, unit="kg")],
    )
    defaults.update(overrides)
    return Order(**defaults)


async def _escalated_order(store, **overrides) -> Order:
    core = OrderProcessingCore(store)
    decision = await core.process(
        _order(
            phone="+919999999999",  # unverified -> escalates
            customer=None,
            **overrides,
        ),
        clarify=False,
    )
    assert not decision.approved
    return await store.get_order(decision.order_id)


async def test_approve_transitions_escalated_order_to_approved(store, core):
    order = await _escalated_order(store)
    assert order.status is OrderStatus.PENDING_REVIEW

    decision = await core.approve_order(
        order.order_id, approved=True, by_phone=APPROVER_PHONE
    )

    assert decision.approved is True
    assert decision.status == OrderStatus.APPROVED.value
    persisted = await store.get_order(order.order_id)
    assert persisted.status is OrderStatus.APPROVED
    assert [e.event_type for e in store.events][-1] == EVENT_ORDER_APPROVED


async def test_reject_transitions_escalated_order_to_terminal_rejected(store, core):
    order = await _escalated_order(store)

    decision = await core.approve_order(
        order.order_id, approved=False, by_phone=APPROVER_PHONE
    )

    assert decision.approved is False
    assert decision.status == OrderStatus.REJECTED.value
    persisted = await store.get_order(order.order_id)
    assert persisted.status is OrderStatus.REJECTED
    assert [e.event_type for e in store.events][-1] == EVENT_ORDER_REJECTED


async def test_non_allowlisted_phone_cannot_decide(core):
    order_id = "ord_x"
    with pytest.raises(ApprovalError):
        await core.approve_order(order_id, approved=True, by_phone=NON_APPROVER_PHONE)


async def test_missing_order_raises(core):
    with pytest.raises(ApprovalError):
        await core.approve_order("ord_missing", approved=True, by_phone=APPROVER_PHONE)


async def test_already_decided_order_cannot_be_approved_again(store, core):
    order = await _escalated_order(store)
    await core.approve_order(order.order_id, approved=True, by_phone=APPROVER_PHONE)

    with pytest.raises(ApprovalError):
        await core.approve_order(
            order.order_id, approved=False, by_phone=APPROVER_PHONE
        )


async def test_rejected_order_stays_visible_for_correction(store, core):
    order = await _escalated_order(store)
    await core.approve_order(order.order_id, approved=False, by_phone=APPROVER_PHONE)

    persisted = await store.get_order(order.order_id)
    assert persisted is not None  # still queryable in the web view
    assert persisted.status is OrderStatus.REJECTED


async def test_web_approve_transitions_escalated_order_to_approved(store, core):
    order = await _escalated_order(store)
    assert order.status is OrderStatus.PENDING_REVIEW

    decision = await core.approve_order_web(order.order_id, approved=True)

    assert decision.approved is True
    assert decision.status == OrderStatus.APPROVED.value
    persisted = await store.get_order(order.order_id)
    assert persisted.status is OrderStatus.APPROVED
    # The same audit event the WhatsApp path writes, recorded with the web actor.
    assert store.events[-1].event_type == EVENT_ORDER_APPROVED
    assert store.events[-1].payload["approved_by"] == "web"


async def test_web_reject_transitions_escalated_order_to_terminal_rejected(store, core):
    order = await _escalated_order(store)

    decision = await core.approve_order_web(order.order_id, approved=False)

    assert decision.approved is False
    assert decision.status == OrderStatus.REJECTED.value
    persisted = await store.get_order(order.order_id)
    assert persisted.status is OrderStatus.REJECTED
    assert store.events[-1].event_type == EVENT_ORDER_REJECTED


async def test_web_approve_missing_order_raises(core):
    with pytest.raises(ApprovalError):
        await core.approve_order_web("ord_missing", approved=True)


async def test_web_approve_already_decided_order_raises(store, core):
    order = await _escalated_order(store)
    await core.approve_order_web(order.order_id, approved=True)

    with pytest.raises(ApprovalError):
        await core.approve_order_web(order.order_id, approved=False)


async def test_web_approve_missing_order_requires_pending_review(store, core):
    clean = _order()  # a clean order auto-approves to approved
    decision = await core.process(clean)
    assert decision.approved is True

    with pytest.raises(ApprovalError):
        await core.approve_order_web(decision.order_id, approved=False)
