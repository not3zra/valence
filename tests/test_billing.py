"""Marking a prepared voucher's order as billed on the core (issue #8).

A prepared voucher's order moves ``approved`` -> ``billed`` (or
``dispatched`` -> ``billed``) through the linear state machine, recorded as an
``order_billed`` Order Event. Marking billed requires a prepared voucher:
billing without the artifact would silently lose it.
"""

from __future__ import annotations

import pytest

from src.core import OrderProcessingCore
from src.orders import (
    EVENT_ORDER_BILLED,
    Order,
    OrderItem,
    OrderStatus,
)
from src.store import InMemoryOrderStore


async def _approved_with_voucher(store, *, order_id: str | None = None) -> str:
    core = OrderProcessingCore(store)
    decision = await core.process(
        Order(
            order_id=order_id,
            phone="+919812345001",
            customer="ChemFab Industries",
            items=[OrderItem(product="sulfuric acid", quantity=2000, unit="kg")],
            delivery_location="Peenya Industrial Area",
            confidence=0.9,
        )
    )
    assert decision.approved
    order = await store.get_order(decision.order_id)
    assert order is not None
    order.voucher_id = f"voucher_{decision.order_id}"
    await store.update_order(order)
    return decision.order_id


async def test_mark_billed_transitions_approved_to_billed():
    store = InMemoryOrderStore()
    order_id = await _approved_with_voucher(store)
    core = OrderProcessingCore(store)

    order = await core.mark_billed(order_id)

    assert order.status is OrderStatus.BILLED
    assert (await store.get_order(order_id)).status is OrderStatus.BILLED
    assert any(
        e.event_type == EVENT_ORDER_BILLED and e.order_id == order_id
        for e in store.events
    )


async def test_mark_billed_from_dispatched():
    store = InMemoryOrderStore()
    order_id = await _approved_with_voucher(store)
    core = OrderProcessingCore(store)
    await core.mark_dispatched(order_id)

    await core.mark_billed(order_id)

    assert (await store.get_order(order_id)).status is OrderStatus.BILLED


async def test_mark_billed_records_the_voucher_id_on_the_event():
    store = InMemoryOrderStore()
    order_id = await _approved_with_voucher(store)
    core = OrderProcessingCore(store)

    await core.mark_billed(order_id)

    event = next(e for e in store.events if e.event_type == EVENT_ORDER_BILLED)
    assert event.payload["voucher_id"] == f"voucher_{order_id}"


async def test_mark_billed_requires_a_prepared_voucher():
    store = InMemoryOrderStore()
    core = OrderProcessingCore(store)
    order_id = await _approved_with_voucher(store)
    order = await store.get_order(order_id)
    assert order is not None
    order.voucher_id = None
    await store.update_order(order)

    with pytest.raises(ValueError, match="voucher"):
        await core.mark_billed(order_id)
    assert (await store.get_order(order_id)).status is OrderStatus.APPROVED


async def test_mark_billed_rejects_a_pending_review_order():
    store = InMemoryOrderStore()
    core = OrderProcessingCore(store)
    decision = await core.process(
        Order(
            phone="+919999999999",
            customer=None,
            items=[OrderItem(product="sulfuric acid", quantity=2000, unit="kg")],
            confidence=0.9,
        )
    )
    assert not decision.approved
    order = await store.get_order(decision.order_id)
    assert order is not None
    order.voucher_id = "voucher_x"
    await store.update_order(order)

    with pytest.raises(ValueError):
        await core.mark_billed(decision.order_id)
    order = await store.get_order(decision.order_id)
    assert order.status is OrderStatus.PENDING_REVIEW


async def test_mark_billed_missing_order_raises():
    core = OrderProcessingCore(InMemoryOrderStore())

    with pytest.raises(ValueError, match="not found"):
        await core.mark_billed("ord_missing")
