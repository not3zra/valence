"""The Order Processing Core — the testable seam everything else feeds into.

Given a structured order the core decides, under the graduated approval policy
(ADR-0002), whether to auto-approve or escalate as a hard block with explicit
reasons; computes the draft estimate from the two-tier money model (driving the
value cap and anomaly checks only — never authoritative); walks the linear
status machine; and appends every decision to the ``order_events`` audit trail.
A repeat of the same order from the same sender inside the configured window
(issue #12) short-circuits to a ``duplicate`` decision — no new order, no
escalation. The thresholds come from the store's config, never hardcoded.
Firestore lives behind the ``OrderStore`` seam and is faked in tests.
"""

from __future__ import annotations

import difflib
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from . import seed_data
from .money import estimate_rate, quantity_is_anomalous, rate_is_anomalous
from .orders import (
    EVENT_ORDER_AUTO_APPROVED,
    EVENT_ORDER_CREATED,
    EVENT_ORDER_DUPLICATE,
    EVENT_ORDER_ESCALATED,
    EscalationReason,
    Order,
    OrderDecision,
    OrderEvent,
    OrderItem,
    OrderStatus,
    transition,
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
    "dedup_window_minutes",
    "clarify_timeout_hours",
    "clarify_turn_cap",
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


def _missing_fields(order: Order) -> list[str]:
    """The user-answerable gaps in an order, for the clarify loop (issue #5).

    Only fields the customer can fill in by replying are listed: the item lines
    (no items at all, or a line missing a product/quantity) and the delivery
    location. The phone always comes from the session identity and never needs
    clarifying.
    """
    missing: list[str] = []
    if not order.items:
        missing.append("items")
    else:
        for item in order.items:
            if (
                not item.product
                or not math.isfinite(item.quantity)
                or item.quantity <= 0
            ):
                missing.append("items")
                break
    if not order.delivery_location:
        missing.append("delivery_location")
    return missing


def _new_order_id() -> str:
    return f"ord_{uuid.uuid4().hex[:12]}"


def _iso_to_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _first_item_matches(
    candidate: OrderItem, prior: OrderItem, products: list[seed_data.Product]
) -> bool:
    """True when two orders share a first line: same product + quantity.

    Cataloged text resolves through the alias table so "Sulphuric Acid" and
    "  sulfuric  acid  " are the same first item (ADR-0003); quantity must
    match exactly — a changed quantity is a fresh order, not a re-send. An
    uncataloged first line is matched by its normalized text, so re-sending
    the same unknown item still dedups.
    """
    if candidate.quantity != prior.quantity:
        return False
    candidate_product = resolve_product(candidate.product, products)
    prior_product = resolve_product(prior.product, products)
    if candidate_product is None and prior_product is None:
        needle = _normalize(candidate.product)
        return bool(needle) and needle == _normalize(prior.product)
    if candidate_product is None or prior_product is None:
        return False
    return candidate_product.id == prior_product.id


class OrderProcessingCore:
    def __init__(self, store: OrderStore) -> None:
        self._store = store

    async def process(
        self, order: Order, *, now: str | None = None, clarify: bool = True
    ) -> OrderDecision:
        """Commit one structured order and return its decision.

        Resolves the customer (phone-exact only), each item's product, and the
        delivery location; collects every escalation reason; computes the draft
        estimate; walks the state machine; persists the order; and appends the
        decision to the audit trail. The thresholds come from the store config;
        a config missing any required key fails loudly (ADR-0002). ``now`` pins
        the intake timestamp for deterministic tests; it defaults to the wall
        clock.

        A repeat of the same order — same sender, matching first item and
        quantity, inside ``dedup_window_minutes`` — is answered with a
        ``duplicate`` decision (issue #12): no order is persisted and no
        escalation reasons are collected, so the agent replies "already
        received" instead of confirming or escalating.
        """
        config = await self._store.get_config()
        missing = [key for key in REQUIRED_CONFIG_KEYS if key not in config]
        if missing:
            raise ConfigurationError(
                "store config missing required key(s): " + ", ".join(missing)
            )
        products = await self._store.get_products()
        if now is None:
            now_dt = datetime.now(timezone.utc)
        else:
            now_dt = _iso_to_dt(now)
        intake_time = now_dt.isoformat()
        order.created_at = intake_time
        order.updated_at = intake_time

        window_minutes = float(config["dedup_window_minutes"])
        duplicate_of = await self._find_duplicate(
            order, products, now_dt, window_minutes
        )
        if duplicate_of is not None:
            prior_id = duplicate_of.order_id or _new_order_id()
            await self._store.append_order_event(
                OrderEvent(
                    order_id=prior_id,
                    event_type=EVENT_ORDER_DUPLICATE,
                    payload={"order_id": prior_id, "phone": order.phone},
                )
            )
            return OrderDecision(
                order_id=order.order_id or _new_order_id(),
                status=OrderStatus.PENDING_REVIEW.value,
                approved=False,
                escalation_reasons=[],
                draft_value_inr=0.0,
                customer_id=None,
                delivery_location_id=None,
                items=[],
                duplicate=True,
                duplicate_of_order_id=prior_id,
            )

        customers = await self._store.get_customers()
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

        missing_fields = _missing_fields(order)
        # Clarify only ever happens over WhatsApp (ADR-0004); a voice order is
        # never held for an answer. And it only applies when the *only* problem
        # is a missing customer-answerable field — any other hard reason
        # (unknown customer, uncataloged product, low confidence, anomaly, over
        # the cap) is an escalation a clarifying question cannot fix.
        is_clarifiable = (
            clarify
            and order.source_channel == "whatsapp"
            and not approved
            and set(escalation_reasons) == {EscalationReason.MISSING_FIELD.value}
            and bool(missing_fields)
        )
        if is_clarifiable:
            return OrderDecision(
                order_id=order.order_id or _new_order_id(),
                status=OrderStatus.PENDING_REVIEW.value,
                approved=False,
                escalation_reasons=[],
                draft_value_inr=draft_total,
                customer_id=customer.id if customer else None,
                delivery_location_id=location.id if location else None,
                items=[_resolved_item(line) for line in resolved],
                clarify=True,
                missing_fields=missing_fields,
            )

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

    async def clarify_policy(self) -> dict:
        """Return the clarify-loop limits read from the store config (ADR-0002).

        A missing key fails loudly, like every other threshold the engine reads.
        """
        config = await self._store.get_config()
        missing = [
            key
            for key in ("clarify_timeout_hours", "clarify_turn_cap")
            if key not in config
        ]
        if missing:
            raise ConfigurationError(
                "store config missing required key(s): " + ", ".join(missing)
            )
        return {
            "clarify_timeout_hours": float(config["clarify_timeout_hours"]),
            "clarify_turn_cap": int(config["clarify_turn_cap"]),
        }

    async def _find_duplicate(
        self,
        order: Order,
        products: list[seed_data.Product],
        now_dt: datetime,
        window_minutes: float,
    ) -> Order | None:
        """Return the prior order this one duplicates, if any (issue #12).

        A prior order from the same sender whose first item and quantity match
        and whose age is strictly inside the configured window makes the new
        order a duplicate. Only the sender's own orders count, so one customer
        re-sending never affects another.
        """
        if not order.items:
            return None
        prior = await self._store.list_orders(phone=order.phone)
        if not prior:
            return None
        first_line = order.items[0]
        best: Order | None = None
        best_at: datetime | None = None
        for earlier in prior:
            if not earlier.items:
                continue
            try:
                earlier_at = _iso_to_dt(earlier.created_at)
            except ValueError:
                continue
            if (now_dt - earlier_at).total_seconds() >= window_minutes * 60:
                continue
            if _first_item_matches(first_line, earlier.items[0], products):
                if best is None or (best_at is not None and earlier_at > best_at):
                    best, best_at = earlier, earlier_at
        return best


def _resolved_item(line: ResolvedLine) -> dict:
    product = line.product
    return {
        "product_id": product.id if product else None,
        "product_name": product.name if product else line.item.product,
        "quantity": line.item.quantity,
        "unit": line.item.unit,
        "rate_inr": line.item.rate_inr,
    }
