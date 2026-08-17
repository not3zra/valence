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
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from . import seed_data
from .loading import is_late_for, parse_business_tz, parse_cutoff
from .money import estimate_rate, quantity_is_anomalous, rate_is_anomalous
from .orders import (
    EVENT_ORDER_APPROVED,
    EVENT_ORDER_AUTO_APPROVED,
    EVENT_ORDER_BILLED,
    EVENT_ORDER_CREATED,
    EVENT_ORDER_DISPATCHED,
    EVENT_ORDER_DUPLICATE,
    EVENT_ORDER_EDITED,
    EVENT_ORDER_ESCALATED,
    EVENT_ORDER_REJECTED,
    EscalationReason,
    Order,
    OrderDecision,
    OrderEvent,
    OrderItem,
    OrderStatus,
    iso_to_dt,
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
    "dedup_window_minutes",
    "clarify_timeout_hours",
    "clarify_turn_cap",
)


class ConfigurationError(RuntimeError):
    """Raised when the store config is missing a key the engine requires."""


class ApprovalError(RuntimeError):
    """Raised when a human approval/rejection cannot be applied to an order."""


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


@dataclass(frozen=True)
class _Evaluation:
    """The graduated-policy outcome for an order (shared by process/edit)."""

    escalation_reasons: list[str]
    draft_total: float
    customer: seed_data.Customer | None
    location: seed_data.DeliveryLocation | None
    resolved: list[ResolvedLine]


# Sentinel distinguishing "key absent" from "explicitly cleared" in edit_order.
_UNSET = object()


def _customer_by_id(
    customer_id: str, customers: list[seed_data.Customer]
) -> seed_data.Customer | None:
    for customer in customers:
        if customer.id == customer_id:
            return customer
    return None


def _product_by_id(
    product_id: str, products: list[seed_data.Product]
) -> seed_data.Product | None:
    for product in products:
        if product.id == product_id:
            return product
    return None


def _record_change(
    diff: dict[str, object], field: str, old: object, new: object
) -> None:
    """Record a before/after change into the ``order_edited`` event payload."""
    diff[field] = {"from": old, "to": new}


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


def _same_product(
    candidate: OrderItem, prior: OrderItem, products: list[seed_data.Product]
) -> bool:
    """True when two item lines state the same product.

    Cataloged text resolves through the alias table so "Sulphuric Acid" and
    "  sulfuric  acid  " are the same product (ADR-0003); an uncataloged line
    is matched by its normalized text. Unlike the dedup first-item check, the
    quantity need not match — the latest statement is authoritative either way.
    """
    candidate_product = resolve_product(candidate.product, products)
    prior_product = resolve_product(prior.product, products)
    if candidate_product is None and prior_product is None:
        needle = _normalize(candidate.product)
        return bool(needle) and needle == _normalize(prior.product)
    if candidate_product is None or prior_product is None:
        return False
    return candidate_product.id == prior_product.id


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
    return _same_product(candidate, prior, products)


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
            now_dt = iso_to_dt(now)
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
                late=False,
            )

        customers = await self._store.get_customers()
        locations = await self._store.get_delivery_locations()

        evaluation = await self._evaluate(
            order,
            config=config,
            products=products,
            customers=customers,
            locations=locations,
        )
        escalation_reasons = evaluation.escalation_reasons
        approved = not escalation_reasons
        customer = evaluation.customer
        location = evaluation.location
        resolved = evaluation.resolved
        draft_total = evaluation.draft_total

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
                late=False,
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
        late = False
        if approved:
            late = await self._is_late_for(order)
        return OrderDecision(
            order_id=order.order_id,
            status=order.status.value,
            approved=approved,
            escalation_reasons=escalation_reasons,
            draft_value_inr=draft_total,
            customer_id=order.customer_id,
            delivery_location_id=order.delivery_location_id,
            items=[_resolved_item(line) for line in resolved],
            late=late,
        )

    async def _evaluate(
        self,
        order: Order,
        *,
        config: dict,
        products: list[seed_data.Product] | None = None,
        customers: list[seed_data.Customer] | None = None,
        locations: list[seed_data.DeliveryLocation] | None = None,
    ) -> _Evaluation:
        """Recompute the graduated-policy outcome for an order.

        Shared by ``process`` (fresh intake) and ``edit_order`` (human
        correction, issue #6), so a web edit re-runs exactly the policy the
        agent ran at intake. A human-resolved ``customer_id`` — set only by the
        web edit path, never at intake — is authoritative identity: an
        approver explicitly assigning a catalog customer to an order clears
        the unknown/unverified reasons and wins over a phone-exact match
        (ADR-0002: the approver is the verification, and their explicit
        mapping is the exception path).
        """
        if products is None:
            products = await self._store.get_products()
        if customers is None:
            customers = await self._store.get_customers()
        if locations is None:
            locations = await self._store.get_delivery_locations()

        reasons: set[str] = set()

        if not order.phone or not order.items:
            reasons.add(EscalationReason.MISSING_FIELD.value)

        if not math.isfinite(order.confidence) or not 0 <= order.confidence <= 1:
            reasons.add(EscalationReason.LOW_CONFIDENCE.value)

        customer = (
            _customer_by_id(order.customer_id, customers)
            if order.customer_id is not None
            else None
        )
        if customer is None:
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
        location = resolve_delivery_location(order.delivery_location, locations)
        return _Evaluation(
            escalation_reasons=escalation_reasons,
            draft_total=draft_total,
            customer=customer,
            location=location,
            resolved=resolved,
        )

    async def merge_held_order(self, held: Order, incoming: Order) -> Order:
        """Merge a fresh extraction into the held partial order (issue #34).

        The held partial accumulates, never gets replaced: new item lines from
        the incoming extraction are appended, a line for a product already in
        the held order is replaced by the latest statement (the same
        alias-aware product match as the dedup seam, ADR-0003), and a delivery
        location or customer supplied in the later reply fills the gap — a
        supplied scalar never overwrites a value the held order already has.
        The phone stays the held session identity; confidence, language, and
        channel come from the incoming extraction, the caller's current
        statement (the model re-reads the whole conversation each turn, so its
        latest confidence covers the accumulated order as a whole).

        The caller then re-runs the merged order through the same evaluation
        as any intake, so an order completed across turns is decided exactly as
        if it had arrived complete in one message. A fresh ``Order`` is
        returned; the held order is left untouched.
        """
        products = await self._store.get_products()
        items = list(held.items)
        for new_item in incoming.items:
            for index, old_item in enumerate(items):
                if _same_product(new_item, old_item, products):
                    items[index] = new_item
                    break
            else:
                items.append(new_item)
        return Order(
            phone=held.phone,
            customer=held.customer or incoming.customer,
            delivery_location=held.delivery_location or incoming.delivery_location,
            confidence=incoming.confidence,
            source_language=incoming.source_language,
            source_channel=incoming.source_channel,
            items=items,
        )

    async def edit_order(self, order_id: str, *, changes: dict) -> Order:
        """Apply an approver's corrections to an order (issue #6, follow-up).

        The review web view's edit actions go through this same Order
        Processing Core the agent and WhatsApp decisions use, so corrections
        stay in sync and land on the shared audit trail as an ``order_edited``
        Order Event. Only an escalated (``pending_review``) or rejected order
        is editable — an approved/dispatched/billed order is locked. An edit to
        a rejected order reopens it to ``pending_review`` for a fresh decision
        (issue #7: rejected orders stay visible for correction).

        ``changes`` may carry any of:

        - ``customer`` / ``delivery_location``: replace the field text (an
          empty string clears it).
        - ``items``: the full replacement line list.
        - ``gst_override_pct``: set (a finite 0..100 percentage) or clear
          (``None``) the per-order GST override the billing path honors
          (issue #8).
        - ``customer_id``: explicitly resolve an unknown customer to a seeded
          catalog customer.
        - ``product_resolutions``: ``[{"index", "product_id"}]`` mapping
          uncataloged item lines to catalog products.

        After any change the order is re-run through the same graduated policy,
        so the approver sees exactly which hard escalation reasons remain; the
        updated ``draft_value_inr`` reflects the corrected lines. ``changes``
        applied with nothing different records no event.
        """
        order = await self._store.get_order(order_id)
        if order is None:
            raise ApprovalError(f"order {order_id} not found")
        if order.status not in (OrderStatus.PENDING_REVIEW, OrderStatus.REJECTED):
            raise ApprovalError(
                f"order {order_id} is {order.status.value}, not editable"
            )

        config = await self._store.get_config()
        missing = [key for key in REQUIRED_CONFIG_KEYS if key not in config]
        if missing:
            raise ConfigurationError(
                "store config missing required key(s): " + ", ".join(missing)
            )

        diff: dict[str, object] = {}

        customer = changes.get("customer", _UNSET)
        if customer is not _UNSET:
            new_customer = (
                None if customer is None else str(customer).strip() or None
            )
            if new_customer != order.customer:
                _record_change(diff, "customer", order.customer, new_customer)
                order.customer = new_customer

        delivery = changes.get("delivery_location", _UNSET)
        if delivery is not _UNSET:
            new_delivery = (
                None if delivery is None else str(delivery).strip() or None
            )
            if new_delivery != order.delivery_location:
                _record_change(
                    diff, "delivery_location", order.delivery_location, new_delivery
                )
                order.delivery_location = new_delivery

        if "items" in changes:
            items = [OrderItem.from_dict(item) for item in changes["items"]]
            items = [item for item in items if item.product.strip()]
            if items != order.items:
                _record_change(
                    diff,
                    "items",
                    [asdict(item) for item in order.items],
                    [asdict(item) for item in items],
                )
                order.items = items

        gst = changes.get("gst_override_pct", _UNSET)
        if gst is not _UNSET:
            new_gst = None if gst is None else float(gst)
            if new_gst is not None and (
                not math.isfinite(new_gst) or not 0 <= new_gst <= 100
            ):
                raise ApprovalError(
                    "GST override must be a percentage between 0 and 100"
                )
            if new_gst != order.gst_override_pct:
                _record_change(
                    diff, "gst_override_pct", order.gst_override_pct, new_gst
                )
                order.gst_override_pct = new_gst

        customers = await self._store.get_customers()
        customer_id = changes.get("customer_id", _UNSET)
        if customer_id is not _UNSET:
            customer_id = str(customer_id).strip() or None
            if customer_id is not None:
                resolved_customer = _customer_by_id(customer_id, customers)
                if resolved_customer is None:
                    raise ApprovalError(f"unknown customer id {customer_id}")
                if order.customer_id != resolved_customer.id:
                    _record_change(
                        diff, "customer", order.customer, resolved_customer.name
                    )
                    diff["resolved_customer_id"] = resolved_customer.id
                    order.customer_id = resolved_customer.id
                    order.customer = resolved_customer.name

        products = await self._store.get_products()
        product_resolutions = changes.get("product_resolutions", [])
        products_resolved: list[dict[str, object]] = []
        for resolution in product_resolutions:
            index = int(resolution["index"])
            if not 0 <= index < len(order.items):
                raise ApprovalError(f"no item at index {index} to resolve")
            product = _product_by_id(str(resolution["product_id"]), products)
            if product is None:
                raise ApprovalError(
                    f"unknown product id {resolution['product_id']}"
                )
            item = order.items[index]
            if item.product != product.name:
                products_resolved.append(
                    {"index": index, "from": item.product, "to": product.name}
                )
                # The stated rate for an unknown product is meaningless once
                # mapped; the authoritative draft price applies instead.
                order.items[index] = OrderItem(
                    product=product.name,
                    quantity=item.quantity,
                    unit=item.unit,
                    rate_inr=None,
                )
        if products_resolved:
            diff["products_resolved"] = products_resolved

        if not diff:
            return order

        evaluation = await self._evaluate(
            order, config=config, products=products, customers=customers
        )
        order.escalation_reasons = evaluation.escalation_reasons
        order.draft_value_inr = evaluation.draft_total
        order.customer_id = evaluation.customer.id if evaluation.customer else None
        order.delivery_location_id = (
            evaluation.location.id if evaluation.location else None
        )

        reopened = order.status is OrderStatus.REJECTED
        if reopened:
            # Reopening a rejected order is an explicit approver correction,
            # not a move of the linear state machine (rejected stays terminal
            # there); it makes the order decidable again after the fix.
            order.status = OrderStatus.PENDING_REVIEW
            _record_change(diff, "status", "rejected", "pending_review")
        order.updated_at = utcnow()

        await self._store.update_order(order)
        await self._store.append_order_event(
            OrderEvent(
                order_id=order.order_id or order_id,
                event_type=EVENT_ORDER_EDITED,
                payload={
                    "changes": diff,
                    "status": order.status.value,
                    "reopened": reopened,
                },
            )
        )
        return order

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

    async def approve_order(
        self, order_id: str, *, approved: bool, by_phone: str
    ) -> OrderDecision:
        """Apply a human yes/no decision to an escalated order (issue #7).

        Only an allowlisted approver may decide; only an order sitting in
        ``pending_review`` (an escalation, never a clean order that was already
        approved) may move. ``approved=True`` transitions to ``approved``,
        ``approved=False`` to the terminal ``rejected`` status — no artifacts
        are generated for a rejected order and it stays visible in the web view
        for correction. Either way the decision is appended to the order's
        audit trail as an ``order_approved`` / ``order_rejected`` event.
        """
        approvers = await self._store.get_approvers()
        if not any(approver.phone == by_phone for approver in approvers):
            raise ApprovalError(
                f"{by_phone} is not an allowlisted approver (issue #7)"
            )
        return await self._apply_human_decision(order_id, approved, by_phone)

    async def approve_order_web(
        self, order_id: str, *, approved: bool
    ) -> OrderDecision:
        """Apply a yes/no decision from the review web view (issue #6).

        The web layer is gated by its own demo passcode, so there is no phone to
        allowlist — the same core decision path as the WhatsApp approval (issue
        #7) is reused, so web and WhatsApp decisions stay in sync and feed the
        same audit trail, with the actor recorded as ``web``.
        """
        return await self._apply_human_decision(order_id, approved, "web")

    async def _is_late_for(self, order: Order) -> bool:
        """Whether a just-approved order landed after the day's cutoff.

        Shared by the intake path and the human-decision path, so an order
        approved late — whether auto-approved at intake or by an approver — is
        flagged the same way and can trigger the dispatch-channel heads-up
        (issue #9). The delivery day is the order's own approval day in the
        business timezone.
        """
        config = await self._store.get_config()
        cutoff = parse_cutoff(config)
        business_tz = parse_business_tz(config)
        delivery_day = iso_to_dt(order.updated_at).astimezone(business_tz).date()
        return is_late_for(order, delivery_day, cutoff, business_tz)

    async def _apply_human_decision(
        self, order_id: str, approved: bool, by: str
    ) -> OrderDecision:
        order = await self._store.get_order(order_id)
        if order is None:
            raise ApprovalError(f"order {order_id} not found")
        if order.status is not OrderStatus.PENDING_REVIEW:
            raise ApprovalError(
                f"order {order_id} is {order.status.value}, not awaiting approval"
            )

        next_status = (
            OrderStatus.APPROVED if approved else OrderStatus.REJECTED
        )
        order.status = transition(OrderStatus.PENDING_REVIEW, next_status)
        order.updated_at = utcnow()
        order_id = order.order_id or order_id

        await self._store.update_order(order)
        event_type = EVENT_ORDER_APPROVED if approved else EVENT_ORDER_REJECTED
        await self._store.append_order_event(
            OrderEvent(
                order_id=order_id,
                event_type=event_type,
                payload={
                    "approved_by": by,
                    "approved": approved,
                },
            )
        )

        return OrderDecision(
            order_id=order_id,
            status=order.status.value,
            approved=approved,
            escalation_reasons=[],
            draft_value_inr=order.draft_value_inr,
            customer_id=order.customer_id,
            delivery_location_id=order.delivery_location_id,
            items=[],
            late=await self._is_late_for(order),
        )

    async def mark_dispatched(self, order_id: str) -> Order:
        """Transition an approved order to dispatched and record the event.

        The order must be in ``approved`` status; the transition to ``dispatched``
        is validated by the state machine (ADR-0002). An ``order_dispatched``
        event is appended to the audit trail. Returns the updated order.
        """
        order = await self._store.get_order(order_id)
        if order is None:
            raise ValueError(f"order {order_id} not found")
        order.status = transition(order.status, OrderStatus.DISPATCHED)
        order.updated_at = utcnow()
        await self._store.update_order(order)
        await self._store.append_order_event(
            OrderEvent(
                order_id=order_id,
                event_type=EVENT_ORDER_DISPATCHED,
                payload={"order_id": order_id},
            )
        )
        return order

    async def mark_billed(self, order_id: str) -> Order:
        """Transition an approved or dispatched order to billed (issue #8).

        Only an order that already has a prepared voucher (``voucher_id`` set)
        may be marked billed — billing without the generated artifact would
        silently lose it. The transition to ``billed`` is validated by the
        state machine (ADR-0002); an ``order_billed`` event is appended to the
        audit trail. Returns the updated order.
        """
        order = await self._store.get_order(order_id)
        if order is None:
            raise ValueError(f"order {order_id} not found")
        if not order.voucher_id:
            raise ValueError(f"order {order_id} has no prepared voucher")
        order.status = transition(order.status, OrderStatus.BILLED)
        order.updated_at = utcnow()
        await self._store.update_order(order)
        await self._store.append_order_event(
            OrderEvent(
                order_id=order_id,
                event_type=EVENT_ORDER_BILLED,
                payload={"voucher_id": order.voucher_id},
            )
        )
        return order

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
                earlier_at = iso_to_dt(earlier.created_at)
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
