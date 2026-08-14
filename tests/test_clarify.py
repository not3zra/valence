"""The clarifying loop (issue #5).

A WhatsApp order whose only problem is a missing customer-answerable field is
answered with a ``clarify`` decision — no order is persisted and no escalation
reasons are collected — so the agent asks for the missing field instead of
confirming or escalating it. Clarify only ever applies over WhatsApp (ADR-0004):
a voice order with a missing field escalates, never waits for an answer. Any
other hard escalation reason (unknown customer, uncataloged product, low
confidence, anomaly, over the cap) also escalates — a clarifying question cannot
fix it. The tool holds the partial order in the durable session and promotes it
to escalation when the turn cap is exceeded or the timeout elapses.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src import seed_data
from src.core import ConfigurationError, OrderProcessingCore
from src.core_tool import CLARIFY_STATE_KEY, build_process_order_tool
from src.orders import EscalationReason, Order, OrderItem, OrderStatus
from src.store import InMemoryOrderStore

CHEMFAB_PHONE = "+919812345001"


class FakeContext:
    def __init__(self, user_id: str):
        self.user_id = user_id
        self.state = {}


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


async def test_missing_delivery_location_clarifies_instead_of_escalating(core, store):
    decision = await core.process(_order(delivery_location=None))

    assert decision.clarify is True
    assert decision.approved is False
    assert decision.escalation_reasons == []
    assert decision.missing_fields == ["delivery_location"]
    # No order is persisted during clarify — the partial lives in the session.
    assert store.orders == []
    assert store.events == []


async def test_empty_items_clarifies(core, store):
    decision = await core.process(_order(items=[]))

    assert decision.clarify is True
    assert decision.missing_fields == ["items"]
    assert store.orders == []


async def test_item_without_quantity_clarifies(core):
    decision = await core.process(
        _order(items=[OrderItem(product="sulfuric acid", quantity=0, unit="kg")])
    )
    assert decision.clarify is True
    assert "items" in decision.missing_fields


async def test_voice_order_never_clarifies(core, store):
    decision = await core.process(
        _order(source_channel="phone", delivery_location=None)
    )
    assert decision.clarify is False
    assert EscalationReason.MISSING_FIELD.value in decision.escalation_reasons
    # Voice orders escalate straight away — they never wait for an answer.
    assert len(store.orders) == 1
    assert store.orders[0].status is OrderStatus.PENDING_REVIEW


async def test_unknown_customer_with_missing_field_escalates_not_clarifies(core, store):
    decision = await core.process(
        _order(phone="+919999999999", customer=None, delivery_location=None)
    )
    assert decision.clarify is False
    assert EscalationReason.UNKNOWN_CUSTOMER.value in decision.escalation_reasons
    assert store.orders


async def test_low_confidence_with_missing_field_escalates_not_clarifies(core):
    decision = await core.process(
        _order(delivery_location=None, confidence=0.2)
    )
    assert decision.clarify is False
    assert EscalationReason.LOW_CONFIDENCE.value in decision.escalation_reasons


async def test_over_cap_with_missing_field_escalates_not_clarifies(core):
    decision = await core.process(
        _order(
            delivery_location=None,
            items=[OrderItem(product="sulfuric acid", quantity=5800, unit="kg")],
        )
    )
    assert decision.clarify is False
    assert EscalationReason.OVER_VALUE_CAP.value in decision.escalation_reasons


async def test_complete_order_does_not_clarify(core):
    decision = await core.process(_order())
    assert decision.clarify is False
    assert decision.approved is True


async def test_clarify_disabled_forces_escalation(core, store):
    decision = await core.process(
        _order(delivery_location=None), clarify=False
    )
    assert decision.clarify is False
    assert decision.approved is False
    assert store.orders[-1].status is OrderStatus.PENDING_REVIEW


async def test_clarify_timeout_hours_is_read_from_config_not_hardcoded():
    store = InMemoryOrderStore(
        config={**seed_data.CONFIG, "clarify_timeout_hours": 48}
    )
    core = OrderProcessingCore(store)
    policy = await core.clarify_policy()
    assert policy["clarify_timeout_hours"] == 48


async def test_clarify_turn_cap_is_read_from_config_not_hardcoded():
    store = InMemoryOrderStore(config={**seed_data.CONFIG, "clarify_turn_cap": 5})
    core = OrderProcessingCore(store)
    policy = await core.clarify_policy()
    assert policy["clarify_turn_cap"] == 5


async def test_missing_clarify_config_fails_loudly():
    config = dict(seed_data.CONFIG)
    del config["clarify_turn_cap"]
    core = OrderProcessingCore(InMemoryOrderStore(config=config))
    with pytest.raises(ConfigurationError, match="clarify_turn_cap"):
        await core.clarify_policy()


def _partial_args():
    # A WhatsApp order missing its delivery location -> clarifiable.
    return {
        "items": [{"product": "sulfuric acid", "quantity": 2000, "unit": "kg"}],
        "confidence": 0.9,
        "source_language": "hi",
        "source_channel": "whatsapp",
    }


async def test_tool_clarifies_and_holds_partial_order_in_session():
    store = InMemoryOrderStore()
    core = OrderProcessingCore(store)
    tool = build_process_order_tool(core)
    ctx = FakeContext(user_id=CHEMFAB_PHONE)

    result = await tool.func(tool_context=ctx, **_partial_args())

    assert result["clarify"] is True
    assert result["missing_fields"] == ["delivery_location"]
    assert result["approved"] is False
    assert store.orders == []
    pending = ctx.state[CLARIFY_STATE_KEY]
    assert pending["turn"] == 1
    assert "created_at" in pending
    assert pending["order"]["phone"] == CHEMFAB_PHONE


async def test_tool_reuses_partial_session_and_increments_turn():
    store = InMemoryOrderStore()
    core = OrderProcessingCore(store)
    tool = build_process_order_tool(core)
    ctx = FakeContext(user_id=CHEMFAB_PHONE)

    first = await tool.func(tool_context=ctx, **_partial_args())
    assert first["clarify"] is True
    # Customer replies, still missing a field -> same session, turn 2.
    second = await tool.func(tool_context=ctx, **_partial_args())

    assert second["clarify"] is True
    assert ctx.state[CLARIFY_STATE_KEY]["turn"] == 2
    assert store.orders == []


async def test_tool_turn_cap_promotes_partial_to_escalation():
    store = InMemoryOrderStore()
    core = OrderProcessingCore(store)
    tool = build_process_order_tool(core)
    ctx = FakeContext(user_id=CHEMFAB_PHONE)

    # cap is 3: the first three partial messages stay in the loop.
    for _ in range(3):
        result = await tool.func(tool_context=ctx, **_partial_args())
        assert result["clarify"] is True
    assert store.orders == []

    # The 4th clarifying turn exceeds the cap -> the held partial escalates.
    fourth = await tool.func(tool_context=ctx, **_partial_args())

    assert fourth["clarify"] is False
    assert fourth["approved"] is False
    assert fourth["status"] == OrderStatus.PENDING_REVIEW.value
    # The loop handed the partial order to a human: an escalated order exists
    # and the session is no longer holding a live partial.
    assert len(store.orders) == 1
    assert store.orders[0].status is OrderStatus.PENDING_REVIEW
    assert ctx.state.get(CLARIFY_STATE_KEY) is None


async def test_tool_completed_order_clears_pending_partial():
    store = InMemoryOrderStore()
    core = OrderProcessingCore(store)
    tool = build_process_order_tool(core)
    ctx = FakeContext(user_id=CHEMFAB_PHONE)

    await tool.func(tool_context=ctx, **_partial_args())
    assert ctx.state.get(CLARIFY_STATE_KEY) is not None

    # Customer supplies the delivery location -> the order commits, and the
    # held partial is no longer current.
    done = await tool.func(
        tool_context=ctx,
        items=[{"product": "sulfuric acid", "quantity": 2000, "unit": "kg"}],
        delivery_location="Peenya Industrial Area",
        confidence=0.9,
        source_language="hi",
        source_channel="whatsapp",
    )

    assert done["approved"] is True
    assert done["clarify"] is False
    assert ctx.state.get(CLARIFY_STATE_KEY) is None
    assert store.orders[-1].status is OrderStatus.APPROVED


async def test_tool_timeout_promotes_pending_partial_to_escalation():
    store = InMemoryOrderStore()
    core = OrderProcessingCore(store)
    tool = build_process_order_tool(core)
    ctx = FakeContext(user_id=CHEMFAB_PHONE)

    await tool.func(tool_context=ctx, **_partial_args())
    # Age the held partial past the 24h timeout, then a fresh message arrives.
    ctx.state[CLARIFY_STATE_KEY]["created_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=48)
    ).isoformat()

    result = await tool.func(
        tool_context=ctx,
        items=[{"product": "toluene", "quantity": 500, "unit": "L"}],
        delivery_location="Whitefield",
        confidence=0.9,
        source_language="en",
        source_channel="whatsapp",
    )

    # The abandoned partial was escalated before the fresh order was handled.
    assert len(store.orders) == 2
    assert store.orders[0].status is OrderStatus.PENDING_REVIEW
    assert store.orders[0].escalation_reasons == ["missing_field"]
    assert ctx.state.get(CLARIFY_STATE_KEY) is None
    # The fresh order itself committed normally.
    assert result["approved"] is True
    assert store.orders[-1].status is OrderStatus.APPROVED
