"""The Order Processing Core — the testable seam everything else feeds into.

Given a structured order the core decides, under the graduated approval policy
(ADR-0002), whether to auto-approve or escalate as a hard block with explicit
reasons; computes the draft estimate from the two-tier money model (driving the
value cap and anomaly checks only — never authoritative); walks the linear
status machine; and appends every decision to the ``order_events`` audit trail.
The thresholds come from the store's config, never hardcoded. Firestore lives
behind the ``OrderStore`` seam and is faked in tests.
"""

from __future__ import annotations

import uuid

from . import seed_data
from .money import estimate_rate, quantity_is_anomalous, rate_is_anomalous
from .orders import (
    EVENT_ORDER_AUTO_APPROVED,
    EVENT_ORDER_CREATED,
    EVENT_ORDER_ESCALATED,
    EscalationReason,
    Order,
    OrderDecision,
    OrderEvent,
    OrderStatus,
    utcnow,
)
from .store import OrderStore

DEFAULT_CONFIG: dict = dict(seed_data.CONFIG)

CANONICAL_REASON_ORDER: list[EscalationReason] = [
    EscalationReason.MISSING_FIELD,
    EscalationReason.UNKNOWN_CUSTOMER,
    EscalationReason.UNVERIFIED_NUMBER,
    EscalationReason.UNCATALOGED_PRODUCT,
    EscalationReason.LOW_CONFIDENCE,
    EscalationReason.OVER_VALUE_CAP,
    EscalationReason.ANOMALY,
]


def _normalize(text: str) -> str:
    return " ".join(text.strip().lower().split())


def resolve_customer(
    phone: str, customers: list[seed_data.Customer]
) -> seed_data.Customer | None:
    """The only credit path: an exact match against a seeded verified phone."""
    for customer in customers:
        if customer.phone == phone:
            return customer
    return None


def resolve_product(
    text: str, products: list[seed_data.Product]
) -> seed_data.Product | None:
    """Match catalog text by id, name, or alias, case-insensitively."""
    needle = _normalize(text)
    if not needle:
        return None
    for product in products:
        candidates = [product.id, product.name, *product.aliases]
        if needle in {_normalize(candidate) for candidate in candidates}:
            return product
    return None


def resolve_delivery_location(
    text: str | None, locations: list[seed_data.DeliveryLocation]
) -> seed_data.DeliveryLocation | None:
    """Match a delivery location by id or name, case-insensitively."""
    if not text:
        return None
    needle = _normalize(text)
    for location in locations:
        candidates = [location.id, location.name]
        if needle in {_normalize(candidate) for candidate in candidates}:
            return location
    return None


def _new_order_id() -> str:
    return f"ord_{uuid.uuid4().hex[:12]}"


class OrderProcessingCore:
    def __init__(self, store: OrderStore) -> None:
        self._store = store

    async def process(self, order: Order) -> OrderDecision:
        config = {**DEFAULT_CONFIG, **(await self._store.get_config())}
        customers = await self._store.get_customers()
        products = await self._store.get_products()
        locations = await self._store.get_delivery_locations()

        reasons: set[str] = set()

        if not order.phone or not order.items:
            reasons.add(EscalationReason.MISSING_FIELD.value)

        customer = resolve_customer(order.phone, customers)
        if customer is None:
            if order.customer:
                reasons.add(EscalationReason.UNVERIFIED_NUMBER.value)
            else:
                reasons.add(EscalationReason.UNKNOWN_CUSTOMER.value)

        resolved: list[tuple] = []
        for item in order.items:
            product = resolve_product(item.product, products)
            if not item.product or item.quantity <= 0:
                reasons.add(EscalationReason.MISSING_FIELD.value)
            if product is None:
                reasons.add(EscalationReason.UNCATALOGED_PRODUCT.value)
            resolved.append((item, product))

        if not order.delivery_location:
            reasons.add(EscalationReason.MISSING_FIELD.value)

        draft_total = 0.0
        for item, product in resolved:
            if product is None:
                continue
            agreed = customer.agreed_rates.get(product.id) if customer else None
            rate = estimate_rate(agreed, None, product.current_price)
            draft_total += rate * item.quantity

        value_cap = float(config["value_cap_inr"])
        if draft_total > value_cap:
            reasons.add(EscalationReason.OVER_VALUE_CAP.value)

        min_confidence = float(config["min_confidence"])
        if order.confidence < min_confidence:
            reasons.add(EscalationReason.LOW_CONFIDENCE.value)

        qty_deviation = float(config["quantity_deviation_above_pct"])
        rate_deviation = float(config["rate_deviation_pct"])
        for item, product in resolved:
            if customer is None or product is None:
                continue
            max_quantity = customer.max_quantities.get(product.id)
            if quantity_is_anomalous(item.quantity, max_quantity, qty_deviation):
                reasons.add(EscalationReason.ANOMALY.value)
            agreed = customer.agreed_rates.get(product.id)
            if rate_is_anomalous(item.rate_inr, agreed, rate_deviation):
                reasons.add(EscalationReason.ANOMALY.value)

        escalation_reasons: list[str] = [
            reason.value for reason in CANONICAL_REASON_ORDER if reason.value in reasons
        ]
        approved = not escalation_reasons

        location = resolve_delivery_location(order.delivery_location, locations)
        order.order_id = order.order_id or _new_order_id()
        order.customer_id = customer.id if customer else None
        order.delivery_location_id = location.id if location else None
        order.draft_value_inr = draft_total
        order.escalation_reasons = escalation_reasons
        order.status = (
            OrderStatus.APPROVED if approved else OrderStatus.PENDING_REVIEW
        )
        order.updated_at = utcnow()

        await self._store.create_order(order)
        await self._store.append_order_event(
            OrderEvent(
                order_id=order.order_id,
                event_type=EVENT_ORDER_CREATED,
                payload={
                    "order_id": order.order_id,
                    "phone": order.phone,
                    "source_channel": order.source_channel,
                    "source_language": order.source_language,
                },
            )
        )
        if approved:
            event_type = EVENT_ORDER_AUTO_APPROVED
            payload: dict = {}
        else:
            event_type = EVENT_ORDER_ESCALATED
            payload = {"order_id": order.order_id, "reasons": escalation_reasons}
        await self._store.append_order_event(
            OrderEvent(
                order_id=order.order_id,
                event_type=event_type,
                payload=payload,
            )
        )

        return OrderDecision(
            order_id=order.order_id,
            status=order.status.value,
            approved=approved,
            escalation_reasons=escalation_reasons,
            draft_value_inr=draft_total,
            customer_id=order.customer_id,
            delivery_location_id=order.delivery_location_id,
            items=[_resolved_item(item, product) for item, product in resolved],
        )


def _resolved_item(item, product: seed_data.Product | None) -> dict:
    return {
        "product_id": product.id if product else None,
        "product_name": product.name if product else item.product,
        "quantity": item.quantity,
        "unit": item.unit,
        "rate_inr": item.rate_inr,
    }
