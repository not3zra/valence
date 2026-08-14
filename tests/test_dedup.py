"""Duplicate-order detection (issue #12).

When the same sender submits a second order with the same first item and
quantity inside the configured window, the core returns a ``duplicate``
decision (no new order, no escalation). The window is read from the store
config (ADR-0002), and a missing key fails loudly. Outside the window, or
with a different first item, the order commits normally.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src import seed_data
from src.core import ConfigurationError, OrderProcessingCore
from src.orders import Order, OrderItem, OrderStatus
from src.store import InMemoryOrderStore

CHEMFAB_PHONE = "+919812345001"


@pytest.fixture
def store():
    return InMemoryOrderStore()


@pytest.fixture
def core(store):
    return OrderProcessingCore(store)


def _order(**overrides) -> Order:
    defaults: dict = dict(
        phone=CHEMFAB_PHONE,
        customer="ChemFab Industries",
        delivery_location="Peenya Industrial Area",
        confidence=0.9,
        source_channel="whatsapp",
        source_language="hi",
        items=[OrderItem(product="sulfuric acid", quantity=2000, unit="kg")],
    )
    defaults.update(overrides)
    return Order(**defaults)


async def test_repeat_within_window_returns_duplicate_and_skips_new_order(core, store):
    first = await core.process(_order())
    assert first.approved is True
    assert first.duplicate is False

    second = await core.process(_order())

    assert second.approved is False
    assert second.duplicate is True
    assert second.duplicate_of_order_id == first.order_id
    assert second.status == OrderStatus.PENDING_REVIEW.value
    assert second.escalation_reasons == []
    assert len(store.orders) == 1
    assert store.events[-1].event_type == "order_duplicated"


async def test_repeat_outside_window_commits_a_fresh_order():
    base = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
    store = InMemoryOrderStore()
    core = OrderProcessingCore(store)
    first = await core.process(
        _order(),
        now=base.isoformat(),
    )

    later = (base + timedelta(minutes=31)).isoformat()
    second = await core.process(_order(), now=later)

    assert first.duplicate is False
    assert second.duplicate is False
    assert second.approved is True
    assert second.order_id != first.order_id
    assert len(store.orders) == 2


async def test_different_first_item_is_not_a_duplicate(core):
    first = await core.process(_order())
    second = await core.process(
        _order(items=[OrderItem(product="toluene", quantity=500, unit="L")])
    )

    assert first.duplicate is False
    assert second.duplicate is False
    assert second.approved is True
    assert second.order_id != first.order_id


async def test_different_quantity_is_not_a_duplicate(core):
    first = await core.process(_order())
    second = await core.process(
        _order(items=[OrderItem(product="sulfuric acid", quantity=2500, unit="kg")])
    )

    assert first.duplicate is False
    assert second.duplicate is False
    assert second.approved is True


async def test_different_sender_is_not_a_duplicate():
    store = InMemoryOrderStore()
    core = OrderProcessingCore(store)
    first = await core.process(_order())
    second = await core.process(_order(phone="+919812345002"))

    assert first.duplicate is False
    assert second.duplicate is False
    assert second.approved is True


async def test_uncataloged_first_item_re_send_is_a_duplicate(core, store):
    first = await core.process(
        _order(items=[OrderItem(product="liquid nitrogen", quantity=10, unit="kg")])
    )
    second = await core.process(
        _order(items=[OrderItem(product="liquid nitrogen", quantity=10, unit="kg")])
    )

    assert first.duplicate is False
    assert second.duplicate is True
    assert second.duplicate_of_order_id == first.order_id
    assert len(store.orders) == 1


async def test_match_is_case_and_whitespace_insensitive_on_product(core):
    first = await core.process(
        _order(items=[OrderItem(product="Sulphuric Acid", quantity=2000, unit="kg")])
    )
    second = await core.process(
        _order(
            items=[
                OrderItem(
                    product="  sulfuric  acid  ", quantity=2000, unit="kg"
                )
            ]
        )
    )

    assert first.duplicate is False
    assert second.duplicate is True
    assert second.duplicate_of_order_id == first.order_id


async def test_dedup_window_is_read_from_config_not_hardcoded():
    base = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
    store = InMemoryOrderStore(config={**seed_data.CONFIG, "dedup_window_minutes": 5})
    core = OrderProcessingCore(store)
    first = await core.process(_order(), now=base.isoformat())

    six_min_later = (base + timedelta(minutes=6)).isoformat()
    second = await core.process(_order(), now=six_min_later)

    assert first.duplicate is False
    assert second.duplicate is False
    assert second.approved is True


async def test_missing_dedup_config_fails_loudly():
    config = dict(seed_data.CONFIG)
    del config["dedup_window_minutes"]
    core = OrderProcessingCore(InMemoryOrderStore(config=config))

    with pytest.raises(ConfigurationError, match="dedup_window_minutes"):
        await core.process(_order())


async def test_repeat_at_exact_window_boundary_is_not_a_duplicate():
    base = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
    store = InMemoryOrderStore()
    core = OrderProcessingCore(store)
    first = await core.process(_order(), now=base.isoformat())

    at_window = (base + timedelta(minutes=30)).isoformat()
    second = await core.process(_order(), now=at_window)

    assert first.duplicate is False
    assert second.duplicate is False
    assert second.approved is True


async def test_escalation_reasons_are_omitted_on_duplicate(core):
    await core.process(_order())
    second = await core.process(
        _order(
            confidence=0.1,
            items=[OrderItem(product="sulfuric acid", quantity=2000)],
        )
    )

    assert second.duplicate is True
    assert second.escalation_reasons == []
