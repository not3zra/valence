"""Editing an order from the review web view (issue #6, follow-up).

The web's edit actions go through the same Order Processing Core the agent and
WhatsApp decisions use, so a correction stays on the shared audit trail as an
``order_edited`` Order Event. Only escalated (``pending_review``) or rejected
orders are editable; after any change the order is re-run through the same
graduated policy, so the approver sees exactly which hard escalation reasons
remain. An explicit resolution — assigning a catalog customer to an unknown
one, or mapping an uncataloged product line to a catalog product — clears the
corresponding reason; a GST override is recorded for the later billing path
(issue #8).
"""

from __future__ import annotations

import pytest

from src.core import ApprovalError, OrderProcessingCore
from src.orders import EscalationReason, Order, OrderItem, OrderStatus
from src.store import InMemoryOrderStore


@pytest.fixture
def store():
    return InMemoryOrderStore()


@pytest.fixture
def core(store):
    return OrderProcessingCore(store)


async def _escalated_order(store) -> str:
    core = OrderProcessingCore(store)
    decision = await core.process(
        Order(
            phone="+919999999999",
            customer=None,
            items=[OrderItem(product="sulfuric acid", quantity=2000, unit="kg")],
            delivery_location=None,
            confidence=0.9,
        )
    )
    return decision.order_id


async def test_edit_changes_customer_and_delivery_location(core, store):
    order_id = await _escalated_order(store)
    assert store.orders[-1].escalation_reasons == [
        EscalationReason.MISSING_FIELD.value,
        EscalationReason.UNKNOWN_CUSTOMER.value,
    ]

    updated = await core.edit_order(
        order_id,
        changes={
            "customer": "ChemFab Industries",
            "delivery_location": "Peenya Industrial Area",
        },
    )

    assert updated.customer == "ChemFab Industries"
    assert updated.delivery_location == "Peenya Industrial Area"
    # A name typed in for an unverified number never credits a customer
    # (ADR-0002); the unknown-customer reason is replaced by unverified-number,
    # and the missing-field reason clears now the location is present.
    assert updated.escalation_reasons == [
        EscalationReason.UNVERIFIED_NUMBER.value
    ]
    assert updated.status is OrderStatus.PENDING_REVIEW

    edited = [e for e in store.events if e.event_type == "order_edited"]
    assert len(edited) == 1
    assert edited[0].payload["changes"]["customer"] == {
        "from": None,
        "to": "ChemFab Industries",
    }


async def test_edit_replaces_items_and_recomputes_draft(core, store):
    core = OrderProcessingCore(store)
    decision = await core.process(
        Order(
            phone="+919812345001",
            customer="ChemFab Industries",
            items=[OrderItem(product="sulfuric acid", quantity=2000, unit="kg")],
            delivery_location=None,
            confidence=0.9,
            source_channel="phone",
        )
    )
    order_id = decision.order_id

    updated = await core.edit_order(
        order_id,
        changes={
            "delivery_location": "Whitefield",
            "items": [
                {"product": "toluene", "quantity": 100, "unit": "L"}
            ],
        },
    )

    assert [item.product for item in updated.items] == ["toluene"]
    assert updated.delivery_location == "Whitefield"
    assert updated.draft_value_inr == pytest.approx(62.0 * 100)
    assert updated.escalation_reasons == []
    assert updated.status is OrderStatus.PENDING_REVIEW


async def test_edit_resolve_unknown_customer_by_id_clears_reason(core, store):
    order_id = await _escalated_order(store)

    updated = await core.edit_order(
        order_id,
        changes={
            "delivery_location": "Peenya Industrial Area",
            "customer_id": "c_chemfab",
        },
    )

    assert updated.customer_id == "c_chemfab"
    assert updated.customer == "ChemFab Industries"
    assert updated.escalation_reasons == []
    edited = [e for e in store.events if e.event_type == "order_edited"]
    assert edited[-1].payload["changes"]["resolved_customer_id"] == "c_chemfab"


async def test_edit_resolve_uncataloged_product_clears_reason(core, store):
    core = OrderProcessingCore(store)
    decision = await core.process(
        Order(
            phone="+919812345001",
            customer="ChemFab Industries",
            items=[OrderItem(product="liquid nitrogen", quantity=10, unit="kg")],
            delivery_location="Peenya Industrial Area",
            confidence=0.9,
        )
    )
    order_id = decision.order_id
    assert EscalationReason.UNCATALOGED_PRODUCT.value in decision.escalation_reasons

    updated = await core.edit_order(
        order_id,
        changes={"product_resolutions": [{"index": 0, "product_id": "p_sulfuric98"}]},
    )

    assert updated.items[0].product == "Sulfuric Acid"
    # The stated rate for an unknown product is meaningless once mapped; the
    # authoritative draft price applies.
    assert updated.items[0].rate_inr is None
    assert updated.escalation_reasons == []
    assert updated.draft_value_inr == pytest.approx(17.5 * 10)


async def test_edit_unknown_product_id_errors(core, store):
    order_id = await _escalated_order(store)
    with pytest.raises(ApprovalError, match="unknown product"):
        await core.edit_order(
            order_id,
            changes={"product_resolutions": [{"index": 0, "product_id": "nope"}]},
        )


async def test_edit_sets_and_clears_gst_override(core, store):
    order_id = await _escalated_order(store)

    set_order = await core.edit_order(order_id, changes={"gst_override_pct": 18.0})
    assert set_order.gst_override_pct == 18.0

    cleared = await core.edit_order(order_id, changes={"gst_override_pct": None})
    assert cleared.gst_override_pct is None
    assert [e.event_type for e in store.events].count("order_edited") == 2


async def test_edit_resolve_customer_overrides_phone_exact_match(core, store):
    core = OrderProcessingCore(store)
    decision = await core.process(
        Order(
            phone="+919812345001",  # phone-exact c_chemfab
            customer="ChemFab Industries",
            items=[OrderItem(product="sulfuric acid", quantity=2000, unit="kg")],
            delivery_location=None,
            confidence=0.9,
            source_channel="phone",
        )
    )
    order_id = decision.order_id
    assert store.orders[-1].customer_id == "c_chemfab"

    updated = await core.edit_order(
        order_id,
        changes={
            "delivery_location": "Whitefield",
            "customer_id": "c_maruthi",
        },
    )

    # The approver's explicit mapping is the exception path (ADR-0002): it wins
    # over the phone-exact match and the customer name follows it.
    assert updated.customer_id == "c_maruthi"
    assert updated.customer == "Maruthi Coatings"
    assert updated.escalation_reasons == []


async def test_edit_invalid_gst_override_errors(core, store):
    order_id = await _escalated_order(store)
    with pytest.raises(ApprovalError, match="GST"):
        await core.edit_order(order_id, changes={"gst_override_pct": 150.0})


async def test_edit_approved_order_is_rejected(core, store):
    core = OrderProcessingCore(store)
    decision = await core.process(
        Order(
            phone="+919812345001",
            customer="ChemFab Industries",
            items=[OrderItem(product="sulfuric acid", quantity=2000, unit="kg")],
            delivery_location="Peenya Industrial Area",
            confidence=0.9,
        )
    )
    assert decision.status == OrderStatus.APPROVED.value

    with pytest.raises(ApprovalError, match="not editable"):
        await core.edit_order(
            decision.order_id, changes={"delivery_location": "Whitefield"}
        )


async def test_edit_rejected_order_reopens_to_pending_review(core, store):
    order_id = await _escalated_order(store)
    await core.approve_order_web(order_id, approved=False)
    assert store.orders[-1].status is OrderStatus.REJECTED

    updated = await core.edit_order(
        order_id, changes={"delivery_location": "Peenya Industrial Area"}
    )

    assert updated.status is OrderStatus.PENDING_REVIEW
    edited = [e for e in store.events if e.event_type == "order_edited"]
    assert edited[-1].payload["changes"]["status"] == {
        "from": "rejected",
        "to": "pending_review",
    }


async def test_edit_missing_order_errors(core):
    with pytest.raises(ApprovalError, match="not found"):
        await core.edit_order("ord_missing", changes={"customer": "X"})


async def test_edit_with_no_changes_records_no_event(core, store):
    order_id = await _escalated_order(store)
    current = store.orders[-1]

    updated = await core.edit_order(order_id, changes={"customer": current.customer})

    assert updated is current
    assert not any(e.event_type == "order_edited" for e in store.events)
