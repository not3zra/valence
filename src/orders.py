"""Order domain: the structured order, its linear status machine, and events.

The order is the normalized shape every intake channel flows into (ADR-0001).
Status moves along the linear chain ``pending_review -> approved -> dispatched
-> billed`` (ADR-0002), an escalated order stays in ``pending_review`` carrying
its escalation reason(s), and ``rejected`` is a terminal status. Every decision
is recorded as an append-only Order Event.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class OrderStatus(str, Enum):
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    DISPATCHED = "dispatched"
    BILLED = "billed"
    REJECTED = "rejected"


class EscalationReason(str, Enum):
    MISSING_FIELD = "missing_field"
    UNKNOWN_CUSTOMER = "unknown_customer"
    UNVERIFIED_NUMBER = "unverified_number"
    UNCATALOGED_PRODUCT = "uncataloged_product"
    LOW_CONFIDENCE = "low_confidence"
    OVER_VALUE_CAP = "over_value_cap"
    ANOMALY = "anomaly"


ALLOWED_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.PENDING_REVIEW: {OrderStatus.APPROVED, OrderStatus.REJECTED},
    OrderStatus.APPROVED: {OrderStatus.DISPATCHED, OrderStatus.BILLED},
    OrderStatus.DISPATCHED: {OrderStatus.BILLED},
    OrderStatus.BILLED: set(),
    OrderStatus.REJECTED: set(),
}


def transition(current: OrderStatus, next_status: OrderStatus) -> OrderStatus:
    """Validate a status move; raise on any illegal transition."""
    if next_status not in ALLOWED_TRANSITIONS[current]:
        raise ValueError(
            f"Illegal order transition: {current.value} -> {next_status.value}"
        )
    return next_status


def is_terminal(status: OrderStatus) -> bool:
    return not ALLOWED_TRANSITIONS[status]


EVENT_ORDER_CREATED = "order_created"
EVENT_ORDER_ESCALATED = "order_escalated"
EVENT_ORDER_AUTO_APPROVED = "order_auto_approved"


@dataclass(frozen=True)
class OrderItem:
    product: str
    quantity: float
    unit: str = ""
    rate_inr: float | None = None


@dataclass
class Order:
    phone: str
    items: list[OrderItem]
    customer: str | None = None
    delivery_location: str | None = None
    confidence: float = 0.0
    source_channel: str = "whatsapp"
    source_language: str = "en"
    order_id: str | None = None
    status: OrderStatus = OrderStatus.PENDING_REVIEW
    escalation_reasons: list[str] = field(default_factory=list)
    customer_id: str | None = None
    delivery_location_id: str | None = None
    draft_value_inr: float = 0.0
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.order_id,
            "phone": self.phone,
            "customer": self.customer,
            "customer_id": self.customer_id,
            "delivery_location": self.delivery_location,
            "delivery_location_id": self.delivery_location_id,
            "items": [asdict(item) for item in self.items],
            "confidence": self.confidence,
            "source_channel": self.source_channel,
            "source_language": self.source_language,
            "status": self.status.value,
            "escalation_reasons": list(self.escalation_reasons),
            "draft_value_inr": self.draft_value_inr,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class OrderEvent:
    order_id: str
    event_type: str
    payload: dict
    created_at: str = field(default_factory=utcnow)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_dict(self) -> dict:
        return {
            "id": self.event_id,
            "order_id": self.order_id,
            "event_type": self.event_type,
            "payload": dict(self.payload),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class OrderDecision:
    order_id: str
    status: str
    approved: bool
    escalation_reasons: list[str]
    draft_value_inr: float
    customer_id: str | None
    delivery_location_id: str | None
    items: list[dict]

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "status": self.status,
            "approved": self.approved,
            "escalation_reasons": list(self.escalation_reasons),
            "draft_value_inr": self.draft_value_inr,
            "customer_id": self.customer_id,
            "delivery_location_id": self.delivery_location_id,
            "items": list(self.items),
        }
