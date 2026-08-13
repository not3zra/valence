"""The Order Processing Core exposed as a single ADK tool.

Asserts the seam the agent and later tickets depend on: the agent carries a
single ``process_order`` tool, and invoking it commits a structured order
through the core with the store (the only external adapter) faked in memory.
The order's phone is pinned to the caller's session identity — a number the
model extracts from the message can never credit a customer (ADR-0002).
"""

from __future__ import annotations

from google.adk.agents import Agent
from google.adk.sessions import InMemorySessionService

from src.agent import build_agent, build_runner, run_turn
from src.core import OrderProcessingCore
from src.core_tool import build_process_order_tool
from src.orders import OrderStatus
from src.store import InMemoryOrderStore

from .fakes import FakeEchoLlm, ToolCallingLlm


class FakeContext:
    def __init__(self, user_id: str):
        self.user_id = user_id


async def test_agent_with_store_exposes_single_process_order_tool():
    agent = build_agent(model=FakeEchoLlm(), store=InMemoryOrderStore())
    assert isinstance(agent, Agent)
    names = [getattr(tool, "name", None) for tool in agent.tools]
    assert names == ["process_order"]


def test_runner_invokes_tool_and_pins_sender_identity():
    store = InMemoryOrderStore()
    agent = build_agent(model=ToolCallingLlm(), store=store)
    runner = build_runner(agent, InMemorySessionService())

    reply = run_turn(runner, sender_id="+919812345001", message="2 tons of acid")

    assert reply == "Order committed."
    assert [(o.phone, o.status, o.customer_id) for o in store.orders] == [
        ("+919812345001", OrderStatus.APPROVED, "c_chemfab")
    ]
    assert [e.event_type for e in store.events] == [
        "order_created",
        "order_auto_approved",
    ]


async def test_process_order_tool_commits_a_clean_order():
    store = InMemoryOrderStore()
    core = OrderProcessingCore(store)
    tool = build_process_order_tool(core)

    result = await tool.func(
        tool_context=FakeContext(user_id="+919812345001"),
        items=[{"product": "sulfuric acid", "quantity": 2000, "unit": "kg"}],
        delivery_location="Peenya Industrial Area",
        confidence=0.9,
        source_language="hi",
    )

    assert result["approved"] is True
    assert result["status"] == OrderStatus.APPROVED.value
    assert result["escalation_reasons"] == []
    assert result["draft_value_inr"] == 17.5 * 2000
    assert store.orders[-1].phone == "+919812345001"
    assert store.orders[-1].status is OrderStatus.APPROVED
    assert [e.event_type for e in store.events] == [
        "order_created",
        "order_auto_approved",
    ]


async def test_process_order_tool_pins_phone_to_session_identity():
    store = InMemoryOrderStore()
    core = OrderProcessingCore(store)
    tool = build_process_order_tool(core)

    result = await tool.func(
        tool_context=FakeContext(user_id="+919812345001"),
        items=[{"product": "sulfuric acid", "quantity": 2000, "unit": "kg"}],
        delivery_location="Peenya Industrial Area",
        confidence=0.9,
    )

    assert result["customer_id"] == "c_chemfab"
    assert store.orders[-1].phone == "+919812345001"


async def test_process_order_tool_escalates_an_unverified_session_number():
    store = InMemoryOrderStore()
    core = OrderProcessingCore(store)
    tool = build_process_order_tool(core)

    result = await tool.func(
        tool_context=FakeContext(user_id="+919999999999"),
        items=[{"product": "sulfuric acid", "quantity": 10}],
        delivery_location="Peenya Industrial Area",
        confidence=0.9,
    )

    assert result["approved"] is False
    assert result["status"] == OrderStatus.PENDING_REVIEW.value
    assert "unknown_customer" in result["escalation_reasons"]
    assert store.orders[-1].phone == "+919999999999"


async def test_process_order_tool_accepts_rate_stated_by_customer():
    store = InMemoryOrderStore()
    core = OrderProcessingCore(store)
    tool = build_process_order_tool(core)

    result = await tool.func(
        tool_context=FakeContext(user_id="+919812345001"),
        items=[{"product": "sulfuric acid", "quantity": 2000, "rate_inr": 25.0}],
        delivery_location="Peenya Industrial Area",
        confidence=0.9,
    )

    assert result["approved"] is False
    assert "anomaly" in result["escalation_reasons"]


async def test_process_order_tool_returns_error_without_session_identity():
    store = InMemoryOrderStore()
    core = OrderProcessingCore(store)
    tool = build_process_order_tool(core)

    result = await tool.func(
        items=[{"product": "sulfuric acid", "quantity": 10}],
        confidence=0.9,
    )

    assert "error" in result
    assert store.orders == []
    assert store.events == []


async def test_process_order_tool_returns_error_on_unparseable_items():
    store = InMemoryOrderStore()
    core = OrderProcessingCore(store)
    tool = build_process_order_tool(core)

    result = await tool.func(
        tool_context=FakeContext(user_id="+919812345001"),
        items=[{"product": "sulfuric acid", "quantity": "not-a-number"}],
        confidence=0.9,
    )

    assert "error" in result
    assert store.orders == []
