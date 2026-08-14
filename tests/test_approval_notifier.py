"""The approval-request notification seam (issue #7).

When an order escalates, every allowlisted approver is told over WhatsApp and
the request is recorded as an ``order_approval_requested`` Order Event — never
as a clarify-loop turn. Only allowlisted numbers get a pending-approval entry,
so only they can ever resolve an order through the approve tool.
"""

from __future__ import annotations

import pytest

from src.approval import ApprovalNotifier
from src.orders import EVENT_ORDER_APPROVAL_REQUESTED
from src.store import InMemoryOrderStore
from src.whatsapp import MockWhatsAppSender

APPROVERS = ["+919845000001", "+919845000002"]


@pytest.fixture
def store():
    return InMemoryOrderStore()


@pytest.fixture
def sender():
    return MockWhatsAppSender()


async def test_escalation_notifies_every_allowlisted_approver(store, sender):
    notifier = ApprovalNotifier(store, sender)
    await notifier.on_order_escalated("ord_x")

    assert {phone for phone, _ in sender.sent} == set(APPROVERS)
    assert all(
        "ord_x" in text for _, text in sender.sent
    )


async def test_escalation_records_request_as_order_event_not_clarify_turn(
    store, sender
):
    notifier = ApprovalNotifier(store, sender)
    await notifier.on_order_escalated("ord_x")

    event = store.events[-1]
    assert event.event_type == EVENT_ORDER_APPROVAL_REQUESTED
    assert event.order_id == "ord_x"
    assert set(event.payload["notified"]) == set(APPROVERS)


async def test_pending_approval_registered_only_for_allowlisted_phones(
    store, sender
):
    notifier = ApprovalNotifier(store, sender)
    await notifier.on_order_escalated("ord_x")

    for approver in APPROVERS:
        assert await store.get_pending_approval(approver) == "ord_x"
    # A customer number that is not an approver has nothing pending.
    assert await store.get_pending_approval("+919812345001") is None
