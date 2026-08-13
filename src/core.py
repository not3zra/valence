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

import difflib
import math
import uuid
from dataclasses import dataclass

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
    OrderItem,
    OrderStatus,
    transition,
    utcnow,
)
from .store import OrderStore

# Every business-judgment threshold the engine reads at runtime (ADR-0002). The
# store config must supply all of them — a missing key fails loudly instead of
# silently falling back to hardcoded defaults.
REQUIRED_CONFIG_KEYS: tuple[str, ...] = (
    "value_cap_inr",
    "min_confidence",
    "quantity_deviation_above_pct",
    "rate_deviation_pct",
)


class ConfigurationError(RuntimeError):
    """Raised when the store config is missing a key the engine requires."""


# After the exact alias table misses (ADR-0003), the normalized product text is
# matched case-insensitively against each product's name and aliases; a score at
# or above this threshold resolves, anything below is an uncataloged product.
FUZZY_MATCH_THRESHOLD = 0.6


CANONICAL_REASON_ORDER: list[EscalationReason] = [
    EscalationReason.MISSING_FIELD,
    EscalationReason.UNKNOWN_CUSTOMER,
    EscalationReason.UNVERIFIED_NUMBER,
    EscalationReason.UNCATALOGED_PRODUCT,
    EscalationReason.LOW_CONFIDENCE,
    EscalationReason.OVER_VALUE_CAP,
    EscalationReason.ANOMALY,
]


@dataclass(frozen=True)
class ResolvedLine:
    item: OrderItem
    product: seed_data.Product | None


def _normalize(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _match(text: str | None, candidates: list[str]) -> bool:
    """True when ``text`` equals any candidate, case- and whitespace-insensitively."""
    needle = _normalize(text) if text else ""
    normalized = {_normalize(candidate) for candidate in candidates}
    return bool(needle) and needle in normalized


def resolve_customer(
    phone: str, customers: list[seed_data.Customer]
) -> seed_data.Customer | None:
    """The only credit path: an exact match against a seeded verified phone."""
    for customer in customers:
        if customer.phone == phone:
            return customer
    return None


def _fuzzy_score(needle: str, candidate: str) -> float:
    return difflib.SequenceMatcher(None, needle, candidate).ratio()


def resolve_product(
    text: str, products: list[seed_data.Product]
) -> seed_data.Product | None:
    """Match catalog text exactly by id, name, or alias, then case-insensitive fuzzy.

    The exact alias table wins first (ADR-0003); on a miss the normalized text is
    scored against every product's name and aliases. Text that clears
    ``FUZZY_MATCH_THRESHOLD`` resolves to the closest product; anything below is
    left as an uncataloged product.
    """
    for product in products:
        if _match(text, [product.id, product.name, *product.aliases]):
            return product

    needle = _normalize(text) if text else ""
    if not needle:
        return None
    best: seed_data.Product | None = None
    best_score = 0.0
    for product in products:
        for candidate in [product.name, *product.aliases]:
            score = _fuzzy_score(needle, _normalize(candidate))
            if score > best_score:
                best, best_score = product, score
    if best is not None and best_score >= FUZZY_MATCH_THRESHOLD:
        return best
    return None


def resolve_delivery_location(
    text: str | None, locations: list[seed_data.DeliveryLocation]
) -> seed_data.DeliveryLocation | None:
    """Match a delivery location by id or name, case-insensitively."""
    for location in locations:
        if _match(text, [location.id, location.name]):
            return location
    return None


def _new_order_id() -> str:
    return f"ord_{uuid.uuid4().hex[:12]}"


class OrderProcessingCore:
    def __init__(self, store: OrderStore) -> None:
        self._store = store

    async def process(self, order: Order) -> OrderDecision:
        """Commit one structured order and return its decision.

        Resolves the customer (phone-exact only), each item's product, and the
        delivery location; collects every escalation reason; computes the draft
        estimate; walks the state machine; persists the order; and appends the
        decision to the audit trail. The thresholds come from the store config;
        a config missing any required key fails loudly (ADR-0002).
        """
        config = await self._store.get_config()
        missing = [key for key in REQUIRED_CONFIG_KEYS if key not in config]
        if missing:
            raise ConfigurationError(
                "store config missing required key(s): " + ", ".join(missing)
            )
        customers = await self._store.get_customers()
        products = await self._store.get_products()
        locations = await self._store.get_delivery_locations()

        reasons: set[str] = set()

        if not order.phone or not order.items:
            reasons.add(EscalationReason.MISSING_FIELD.value)

        if not math.isfinite(order.confidence) or not 0 <= order.confidence <= 1:
            reasons.add(EscalationReason.LOW_CONFIDENCE.value)

        customer = resolve_customer(order.phone, customers)
        if customer is None:
            if order.customer:
                reasons.add(EscalationReason.UNVERIFIED_NUMBER.value)
            else:
                reasons.add(EscalationReason.UNKNOWN_CUSTOMER.value)

        resolved: list[ResolvedLine] = []
        for item in order.items:
            product = resolve_product(item.product, products)
            missing_quantity = not math.isfinite(item.quantity) or item.quantity <= 0
            if not item.product or missing_quantity:
                reasons.add(EscalationReason.MISSING_FIELD.value)
            if product is None:
                reasons.add(EscalationReason.UNCATALOGED_PRODUCT.value)
            resolved.append(ResolvedLine(item=item, product=product))

        if not order.delivery_location:
            reasons.add(EscalationReason.MISSING_FIELD.value)

        draft_total = 0.0
        for line in resolved:
            if line.product is None:
                continue
            agreed = customer.agreed_rates.get(line.product.id) if customer else None
            rate = estimate_rate(
                agreed, line.product.tier_price, line.product.current_price
            )
            draft_total += rate * line.item.quantity

        value_cap = float(config["value_cap_inr"])
        if draft_total > value_cap:
            reasons.add(EscalationReason.OVER_VALUE_CAP.value)

        min_confidence = float(config["min_confidence"])
        if order.confidence < min_confidence:
            reasons.add(EscalationReason.LOW_CONFIDENCE.value)

        qty_deviation = float(config["quantity_deviation_above_pct"])
        rate_deviation = float(config["rate_deviation_pct"])
        for line in resolved:
            if customer is None or line.product is None:
                continue
            max_quantity = customer.max_quantities.get(line.product.id)
            if quantity_is_anomalous(line.item.quantity, max_quantity, qty_deviation):
                reasons.add(EscalationReason.ANOMALY.value)
            agreed = customer.agreed_rates.get(line.product.id)
            if rate_is_anomalous(line.item.rate_inr, agreed, rate_deviation):
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
            transition(OrderStatus.PENDING_REVIEW, OrderStatus.APPROVED)
            if approved
            else OrderStatus.PENDING_REVIEW
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
            payload: dict[str, object] = {}
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
            items=[_resolved_item(line) for line in resolved],
        )


def _resolved_item(line: ResolvedLine) -> dict:
    product = line.product
    return {
        "product_id": product.id if product else None,
        "product_name": product.name if product else line.item.product,
        "quantity": line.item.quantity,
        "unit": line.item.unit,
        "rate_inr": line.item.rate_inr,
    }
