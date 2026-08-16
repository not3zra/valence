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
from src.core_tool import (
    build_approve_order_tool,
    build_prepare_voucher_tool,
    build_process_order_tool,
)
from src.dispatch import LateOrderNotifier
from src.orders import EVENT_ORDER_LATE, Order, OrderItem, OrderStatus
from src.seed_data import CONFIG
from src.store import InMemoryOrderStore
from src.voucher import InMemoryVoucherStore
from src.whatsapp import MockWhatsAppSender

from .fakes import FakeEchoLlm, ToolCallingLlm, approved_order_id


class FakeContext:
    def __init__(self, user_id: str):
        self.user_id = user_id


class _RecordingNotifier:
    def __init__(self):
        self.escalations: list[str] = []

    async def on_order_escalated(self, order_id: str) -> None:
        self.escalations.append(order_id)


async def test_agent_with_store_exposes_core_tools():
    agent = build_agent(model=FakeEchoLlm(), store=InMemoryOrderStore())
    assert isinstance(agent, Agent)
    names = [getattr(tool, "name", None) for tool in agent.tools]
    assert names == [
        "process_order",
        "approve_order",
        "prepare_voucher",
        "render_loading_list",
    ]


def test_runner_invokes_tool_and_pins_sender_identity():
    # A late cutoff keeps the approval on-time at any wall clock, so the late
    # notifier never fires and the event trail stays deterministic.
    store = InMemoryOrderStore(config={**CONFIG, "cutoff_time": "23:59"})
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


async def test_process_order_tool_notifies_dispatch_for_a_late_approved_order():
    # A cutoff of 00:00 makes any approval time today late (issue #9): an
    # auto-approved order after it triggers the dispatch-channel heads-up.
    store = InMemoryOrderStore(config={**CONFIG, "cutoff_time": "00:00"})
    sender = MockWhatsAppSender()
    core = OrderProcessingCore(store)
    tool = build_process_order_tool(
        core, late_notifier=LateOrderNotifier(store, sender)
    )

    result = await tool.func(
        tool_context=FakeContext(user_id="+919812345001"),
        items=[{"product": "sulfuric acid", "quantity": 2000, "unit": "kg"}],
        delivery_location="Peenya Industrial Area",
        confidence=0.9,
    )

    assert result["approved"] is True
    assert result["late"] is True
    assert len(sender.sent) == 1
    assert sender.sent[0][1].startswith("Late order ")
    assert [e.event_type for e in store.events[-1:]] == [EVENT_ORDER_LATE]


async def test_process_order_tool_skips_notifier_for_an_on_time_order():
    # A cutoff of 23:59 keeps any approval time today inside the window.
    store = InMemoryOrderStore(config={**CONFIG, "cutoff_time": "23:59"})
    sender = MockWhatsAppSender()
    core = OrderProcessingCore(store)
    tool = build_process_order_tool(
        core, late_notifier=LateOrderNotifier(store, sender)
    )

    result = await tool.func(
        tool_context=FakeContext(user_id="+919812345001"),
        items=[{"product": "sulfuric acid", "quantity": 2000, "unit": "kg"}],
        delivery_location="Peenya Industrial Area",
        confidence=0.9,
    )

    assert result["approved"] is True
    assert result["late"] is False
    assert sender.sent == []
    assert [e.event_type for e in store.events] == [
        "order_created",
        "order_auto_approved",
    ]


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


async def _escalate(store) -> str:
    core = OrderProcessingCore(store)
    decision = await core.process(
        Order(
            phone="+919999999999",
            customer=None,
            items=[OrderItem(product="sulfuric acid", quantity=2000, unit="kg")],
            confidence=0.9,
        ),
        clarify=False,
    )
    assert not decision.approved
    await store.set_pending_approval("+919845000001", decision.order_id)
    return decision.order_id


async def test_approve_order_tool_approves_the_approvers_pending_order():
    store = InMemoryOrderStore()
    core = OrderProcessingCore(store)
    order_id = await _escalate(store)
    tool = build_approve_order_tool(core, store)

    result = await tool.func(
        approved=True, tool_context=FakeContext(user_id="+919845000001")
    )

    assert result["approved"] is True
    assert result["order_id"] == order_id
    assert (await store.get_order(order_id)).status is OrderStatus.APPROVED
    assert await store.get_pending_approval("+919845000001") is None


async def test_approve_order_tool_rejects_the_approvers_pending_order():
    store = InMemoryOrderStore()
    core = OrderProcessingCore(store)
    await _escalate(store)
    tool = build_approve_order_tool(core, store)

    result = await tool.func(
        approved=False, tool_context=FakeContext(user_id="+919845000001")
    )

    assert result["approved"] is False
    assert result["status"] == OrderStatus.REJECTED.value


async def test_approve_order_tool_returns_error_for_non_approver_number():
    store = InMemoryOrderStore()
    core = OrderProcessingCore(store)
    await _escalate(store)
    tool = build_approve_order_tool(core, store)

    # A real customer number that is not an allowlisted approver has nothing
    # pending and can never act — the agent is told to ignore the reply.
    result = await tool.func(
        approved=True, tool_context=FakeContext(user_id="+919812345001")
    )

    assert "error" in result
    # Nothing was approved for that number's order.
    assert not any(
        o.status is OrderStatus.APPROVED for o in store.orders
    )


async def test_approve_order_tool_returns_error_without_identity():
    store = InMemoryOrderStore()
    core = OrderProcessingCore(store)
    tool = build_approve_order_tool(core, store)

    result = await tool.func(approved=True)

    assert "error" in result


async def test_approve_order_tool_clears_pending_so_second_reply_cannot_act():
    store = InMemoryOrderStore()
    core = OrderProcessingCore(store)
    await _escalate(store)
    tool = build_approve_order_tool(core, store)

    await tool.func(approved=True, tool_context=FakeContext(user_id="+919845000001"))
    second = await tool.func(
        approved=False, tool_context=FakeContext(user_id="+919845000001")
    )

    assert "error" in second  # nothing pending for a second decision


async def test_approve_order_tool_notifies_dispatch_for_a_late_approval():
    # A human approval landing after a 00:00 cutoff is late (issue #9): the
    # approve tool must fire the same dispatch-channel heads-up as the intake
    # path, not only auto-approved orders.
    store = InMemoryOrderStore(config={**CONFIG, "cutoff_time": "00:00"})
    core = OrderProcessingCore(store)
    await _escalate(store)
    sender = MockWhatsAppSender()
    tool = build_approve_order_tool(
        core, store, late_notifier=LateOrderNotifier(store, sender)
    )

    result = await tool.func(
        approved=True, tool_context=FakeContext(user_id="+919845000001")
    )

    assert result["approved"] is True
    assert result["late"] is True
    assert len(sender.sent) == 1
    assert sender.sent[0][1].startswith("Late order ")
    assert [e.event_type for e in store.events[-1:]] == [EVENT_ORDER_LATE]


async def test_approve_order_tool_skips_notifier_for_an_on_time_approval():
    store = InMemoryOrderStore(config={**CONFIG, "cutoff_time": "23:59"})
    core = OrderProcessingCore(store)
    await _escalate(store)
    sender = MockWhatsAppSender()
    tool = build_approve_order_tool(
        core, store, late_notifier=LateOrderNotifier(store, sender)
    )

    result = await tool.func(
        approved=True, tool_context=FakeContext(user_id="+919845000001")
    )

    assert result["approved"] is True
    assert result["late"] is False
    assert sender.sent == []


async def test_notifier_only_fires_for_escalations():
    # A clean order is auto-approved and a duplicate never needs a human
    # decision — neither should notify any approver (issue #7 seam contract).
    store = InMemoryOrderStore()
    core = OrderProcessingCore(store)
    notifier = _RecordingNotifier()
    tool = build_process_order_tool(core, notifier=notifier)

    clean = await tool.func(
        tool_context=FakeContext(user_id="+919812345001"),
        items=[{"product": "sulfuric acid", "quantity": 2000, "unit": "kg"}],
        delivery_location="Peenya Industrial Area",
        confidence=0.9,
    )
    assert clean["approved"] is True
    assert notifier.escalations == []

    duplicate = await tool.func(
        tool_context=FakeContext(user_id="+919812345001"),
        items=[{"product": "sulfuric acid", "quantity": 2000, "unit": "kg"}],
        delivery_location="Peenya Industrial Area",
        confidence=0.9,
    )
    assert duplicate["duplicate"] is True
    assert notifier.escalations == []

    escalated = await tool.func(
        tool_context=FakeContext(user_id="+919999999999"),
        items=[{"product": "sulfuric acid", "quantity": 2000, "unit": "kg"}],
        confidence=0.9,
    )
    assert escalated["approved"] is False
    assert notifier.escalations == [escalated["order_id"]]


async def test_prepare_voucher_tool_generates_and_returns_the_voucher():
    store = InMemoryOrderStore()
    storage = InMemoryVoucherStore()
    order_id = await approved_order_id(store)
    tool = build_prepare_voucher_tool(store, storage)

    result = await tool.func(order_id=order_id)

    assert "error" not in result
    assert result["order_id"] == order_id
    assert result["party_ledger"] == "CHEMFAB INDUSTRIES"
    assert result["gst_type"] == "CGST"
    assert result["voucher_id"] == f"voucher_{order_id}"
    assert storage.blobs[result["voucher_id"]]  # stored XML
    assert (await store.get_order(order_id)).voucher_id == result["voucher_id"]
    assert any(
        e.event_type == "voucher_ready" and e.order_id == order_id
        for e in store.events
    )


async def test_prepare_voucher_tool_returns_error_for_a_not_approved_order():
    store = InMemoryOrderStore()
    storage = InMemoryVoucherStore()
    core = OrderProcessingCore(store)
    decision = await core.process(
        Order(
            phone="+919999999999",
            customer=None,
            items=[OrderItem(product="sulfuric acid", quantity=2000, unit="kg")],
            confidence=0.9,
        )
    )
    tool = build_prepare_voucher_tool(store, storage)

    result = await tool.func(order_id=decision.order_id)

    assert "error" in result
    assert "not approved" in result["error"]
    assert storage.blobs == {}


async def test_prepare_voucher_tool_returns_error_for_an_unmapped_master():
    store = InMemoryOrderStore()
    storage = InMemoryVoucherStore()
    order_id = await approved_order_id(store)
    tool = build_prepare_voucher_tool(store, storage)
    store.products = []

    result = await tool.func(order_id=order_id)

    assert "error" in result
    assert "not mapped" in result["error"]
    assert storage.blobs == {}
