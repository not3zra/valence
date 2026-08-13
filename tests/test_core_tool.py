"""The Order Processing Core exposed as a single ADK tool.

Asserts the seam the agent and later tickets depend on: the agent carries a
single ``process_order`` tool, and invoking it commits a structured order
through the core with the store (the only external adapter) faked in memory.
"""

from __future__ import annotations

from google.adk.agents import Agent

from src.agent import build_agent
from src.core import OrderProcessingCore
from src.core_tool import build_process_order_tool
from src.orders import OrderStatus
from src.store import InMemoryOrderStore

from .fakes import FakeEchoLlm


async def test_agent_with_store_exposes_single_process_order_tool():
    agent = build_agent(model=FakeEchoLlm(), store=InMemoryOrderStore())
    assert isinstance(agent, Agent)
    names = [getattr(tool, "name", None) for tool in agent.tools]
    assert names == ["process_order"]


async def test_process_order_tool_commits_a_clean_order():
    store = InMemoryOrderStore()
    core = OrderProcessingCore(store)
    tool = build_process_order_tool(core)

    result = await tool.func(
        phone="+919812345001",
        customer="ChemFab Industries",
        items=[{"product": "sulfuric acid", "quantity": 2000, "unit": "kg"}],
        delivery_location="Peenya Industrial Area",
        confidence=0.9,
        source_language="hi",
    )

    assert result["approved"] is True
    assert result["status"] == OrderStatus.APPROVED.value
    assert result["escalation_reasons"] == []
    assert result["draft_value_inr"] == 17.5 * 2000
    assert store.orders[-1].status is OrderStatus.APPROVED
    assert [e.event_type for e in store.events] == [
        "order_created",
        "order_auto_approved",
    ]


async def test_process_order_tool_escalates_an_unknown_customer():
    store = InMemoryOrderStore()
    core = OrderProcessingCore(store)
    tool = build_process_order_tool(core)

    result = await tool.func(
        phone="+919999999999",
        items=[{"product": "sulfuric acid", "quantity": 10}],
        delivery_location="Peenya",
        confidence=0.9,
    )

    assert result["approved"] is False
    assert result["status"] == OrderStatus.PENDING_REVIEW.value
    assert "unknown_customer" in result["escalation_reasons"]
    assert store.orders[-1].status is OrderStatus.PENDING_REVIEW


async def test_process_order_tool_accepts_rate_stated_by_customer():
    store = InMemoryOrderStore()
    core = OrderProcessingCore(store)
    tool = build_process_order_tool(core)

    result = await tool.func(
        phone="+919812345001",
        items=[{"product": "sulfuric acid", "quantity": 2000, "rate_inr": 25.0}],
        delivery_location="Peenya",
        confidence=0.9,
    )

    assert result["approved"] is False
    assert "anomaly" in result["escalation_reasons"]
