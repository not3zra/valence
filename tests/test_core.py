"""The Order Processing Core decision engine.

Every escalation reason class is exercised as a hard block, the auto-approve
path writes the status transition plus an audit event, and the thresholds are
proven to come from the store's config — never hardcoded. The store is the
in-memory fake, so no Firestore or Gemini is involved.
"""

from __future__ import annotations

import pytest

from src import seed_data
from src.core import OrderProcessingCore, resolve_product
from src.orders import EscalationReason, Order, OrderItem, OrderStatus
from src.store import InMemoryOrderStore

CHEMFAB_PHONE = "+919812345001"  # c_chemfab


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


async def test_clean_order_auto_approves_and_persists_transition(core, store):
    decision = await core.process(_order())

    assert decision.approved is True
    assert decision.status == OrderStatus.APPROVED.value
    assert decision.escalation_reasons == []
    assert decision.customer_id == "c_chemfab"
    assert decision.draft_value_inr == pytest.approx(17.5 * 2000)

    persisted = store.orders[-1]
    assert persisted.status is OrderStatus.APPROVED
    assert [e.event_type for e in store.events] == [
        "order_created",
        "order_auto_approved",
    ]
    assert all(e.order_id == decision.order_id for e in store.events)


async def test_clean_order_resolves_delivery_location_to_seeded_id(core):
    decision = await core.process(_order())
    assert decision.delivery_location_id == "dl_peenya"


async def test_unknown_customer_escalates_as_hard_block(core):
    decision = await core.process(
        _order(phone="+919999999999", customer=None)
    )
    assert decision.approved is False
    assert decision.status == OrderStatus.PENDING_REVIEW.value
    assert decision.escalation_reasons == [EscalationReason.UNKNOWN_CUSTOMER.value]


async def test_unverified_number_escalates_even_with_a_matching_name(core):
    decision = await core.process(
        _order(phone="+919999999999", customer="ChemFab Industries")
    )
    assert EscalationReason.UNVERIFIED_NUMBER.value in decision.escalation_reasons
    assert decision.customer_id is None


async def test_uncataloged_product_escalates(core):
    decision = await core.process(
        _order(items=[OrderItem(product="liquid nitrogen", quantity=10, unit="kg")])
    )
    assert EscalationReason.UNCATALOGED_PRODUCT.value in decision.escalation_reasons


async def test_missing_delivery_location_escalates(core):
    decision = await core.process(_order(delivery_location=None))
    assert EscalationReason.MISSING_FIELD.value in decision.escalation_reasons


async def test_empty_items_escalates(core):
    decision = await core.process(_order(items=[]))
    assert EscalationReason.MISSING_FIELD.value in decision.escalation_reasons


async def test_item_without_quantity_escalates(core):
    decision = await core.process(
        _order(items=[OrderItem(product="sulfuric acid", quantity=0, unit="kg")])
    )
    assert EscalationReason.MISSING_FIELD.value in decision.escalation_reasons


async def test_non_finite_quantity_escalates(core):
    decision = await core.process(
        _order(items=[OrderItem(product="sulfuric acid", quantity=float("nan"))])
    )
    assert EscalationReason.MISSING_FIELD.value in decision.escalation_reasons


async def test_low_confidence_escalates(core):
    decision = await core.process(_order(confidence=0.3))
    assert EscalationReason.LOW_CONFIDENCE.value in decision.escalation_reasons


async def test_non_finite_confidence_escalates(core):
    decision = await core.process(_order(confidence=float("nan")))
    assert EscalationReason.LOW_CONFIDENCE.value in decision.escalation_reasons


async def test_out_of_range_confidence_escalates(core):
    decision = await core.process(_order(confidence=1.5))
    assert EscalationReason.LOW_CONFIDENCE.value in decision.escalation_reasons


async def test_value_above_cap_escalates_but_without_anomaly(core):
    decision = await core.process(
        _order(items=[OrderItem(product="sulfuric acid", quantity=5800, unit="kg")])
    )
    assert EscalationReason.OVER_VALUE_CAP.value in decision.escalation_reasons
    assert EscalationReason.ANOMALY.value not in decision.escalation_reasons


async def test_quantity_anomaly_escalates(core):
    decision = await core.process(
        _order(items=[OrderItem(product="sulfuric acid", quantity=7000, unit="kg")])
    )
    assert EscalationReason.ANOMALY.value in decision.escalation_reasons


async def test_rate_anomaly_escalates(core):
    decision = await core.process(
        _order(
            items=[
                OrderItem(
                    product="sulfuric acid",
                    quantity=2000,
                    unit="kg",
                    rate_inr=25.0,
                )
            ]
        )
    )
    assert EscalationReason.ANOMALY.value in decision.escalation_reasons


async def test_escalated_order_stays_pending_review_and_records_events(core, store):
    decision = await core.process(_order(phone="+919999999999", customer=None))

    persisted = store.orders[-1]
    assert persisted.status is OrderStatus.PENDING_REVIEW
    assert persisted.escalation_reasons == decision.escalation_reasons
    assert [e.event_type for e in store.events] == ["order_created", "order_escalated"]
    assert store.events[-1].payload["reasons"] == decision.escalation_reasons


async def test_multiple_reasons_recorded_in_canonical_order(core):
    decision = await core.process(
        _order(phone="+919999999999", customer=None, confidence=0.2)
    )
    assert decision.escalation_reasons == [
        EscalationReason.UNKNOWN_CUSTOMER.value,
        EscalationReason.LOW_CONFIDENCE.value,
    ]


async def test_draft_estimate_prefers_agreed_rate_over_catalog(core):
    decision = await core.process(
        _order(items=[OrderItem(product="sulfuric acid", quantity=2000, unit="kg")])
    )
    assert decision.draft_value_inr == pytest.approx(17.5 * 2000)


async def test_draft_estimate_falls_back_to_catalog_price():
    store = InMemoryOrderStore()
    core = OrderProcessingCore(store)
    decision = await core.process(
        _order(
            phone="+919812345004",  # c_anand — no agreed rate for toluene
            customer="Anand Agro Chem",
            items=[OrderItem(product="toluene", quantity=100, unit="L")],
        )
    )
    assert decision.draft_value_inr == pytest.approx(62.0 * 100)


async def test_value_cap_is_read_from_config_not_hardcoded():
    store = InMemoryOrderStore(
        config={**seed_data.CONFIG, "value_cap_inr": 30000}
    )
    core = OrderProcessingCore(store)
    decision = await core.process(_order())
    assert EscalationReason.OVER_VALUE_CAP.value in decision.escalation_reasons


async def test_confidence_threshold_is_read_from_config_not_hardcoded():
    store = InMemoryOrderStore(
        config={**seed_data.CONFIG, "min_confidence": 0.95}
    )
    core = OrderProcessingCore(store)
    decision = await core.process(_order(confidence=0.9))
    assert EscalationReason.LOW_CONFIDENCE.value in decision.escalation_reasons


async def test_quantity_deviation_threshold_is_read_from_config_not_hardcoded():
    store = InMemoryOrderStore(
        config={**seed_data.CONFIG, "quantity_deviation_above_pct": 0.1}
    )
    core = OrderProcessingCore(store)
    decision = await core.process(
        _order(items=[OrderItem(product="sulfuric acid", quantity=4500, unit="kg")])
    )
    assert EscalationReason.ANOMALY.value in decision.escalation_reasons


def test_resolve_product_matches_alias_case_insensitively():
    product = resolve_product("Sulphuric Acid", seed_data.PRODUCTS)
    assert product is not None
    assert product.id == "p_sulfuric98"


def test_resolve_product_matches_by_canonical_id():
    product = resolve_product("p_hcl", seed_data.PRODUCTS)
    assert product is not None
    assert product.id == "p_hcl"


def test_resolve_product_returns_none_for_unmatched_text():
    assert resolve_product("liquid nitrogen", seed_data.PRODUCTS) is None
