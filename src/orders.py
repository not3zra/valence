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


def iso_to_dt(value: str) -> datetime:
    """Parse an ISO timestamp, treating a naive value as UTC."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


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
    UNCATALOGED_LOCATION = "uncataloged_location"
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
EVENT_ORDER_DUPLICATE = "order_duplicated"
EVENT_ORDER_APPROVAL_REQUESTED = "order_approval_requested"
EVENT_ORDER_APPROVED = "order_approved"
EVENT_ORDER_REJECTED = "order_rejected"
EVENT_ORDER_EDITED = "order_edited"
EVENT_ORDER_DISPATCHED = "order_dispatched"
EVENT_ORDER_LATE = "order_late"
EVENT_VOUCHER_READY = "voucher_ready"
EVENT_ORDER_BILLED = "order_billed"


@dataclass(frozen=True)
class OrderItem:
    product: str
    quantity: float
    unit: str = ""
    rate_inr: float | None = None

    @classmethod
    def from_dict(cls, data: dict) -> OrderItem:
        """Build a line from extracted JSON, tolerating missing keys."""
        return cls(
            product=str(data.get("product", "")),
            quantity=float(data.get("quantity", 0.0)),
            unit=str(data.get("unit", "")),
            rate_inr=(
                float(data["rate_inr"]) if data.get("rate_inr") is not None else None
            ),
        )


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
    gst_override_pct: float | None = None
    voucher_id: str | None = None
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
            "gst_override_pct": self.gst_override_pct,
            "voucher_id": self.voucher_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Order:
        """Rebuild an Order from a stored document (``to_dict`` round-trip)."""
        return cls(
            order_id=data.get("id"),
            phone=str(data.get("phone", "")),
            customer=data.get("customer"),
            customer_id=data.get("customer_id"),
            delivery_location=data.get("delivery_location"),
            delivery_location_id=data.get("delivery_location_id"),
            items=[OrderItem.from_dict(item) for item in data.get("items", [])],
            confidence=float(data.get("confidence", 0.0)),
            source_channel=str(data.get("source_channel", "whatsapp")),
            source_language=str(data.get("source_language", "en")),
            status=OrderStatus(data.get("status", OrderStatus.PENDING_REVIEW.value)),
            escalation_reasons=list(data.get("escalation_reasons", [])),
            draft_value_inr=float(data.get("draft_value_inr", 0.0)),
            gst_override_pct=(
                float(data["gst_override_pct"])
                if data.get("gst_override_pct") is not None
                else None
            ),
            voucher_id=data.get("voucher_id"),
            created_at=str(data.get("created_at", utcnow())),
            updated_at=str(data.get("updated_at", utcnow())),
        )


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

    @classmethod
    def from_dict(cls, data: dict) -> OrderEvent:
        """Rebuild an OrderEvent from a stored document (``to_dict`` round-trip)."""
        return cls(
            order_id=str(data.get("order_id", "")),
            event_type=str(data.get("event_type", "")),
            payload=dict(data.get("payload", {})),
            created_at=str(data.get("created_at", utcnow())),
            event_id=str(data.get("id", uuid.uuid4().hex)),
        )


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
    duplicate: bool = False
    duplicate_of_order_id: str | None = None
    clarify: bool = False
    missing_fields: list[str] = field(default_factory=list)
    late: bool = False
    unavailable_items: list[str] = field(default_factory=list)
    reply_hint: str = ""

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
            "duplicate": self.duplicate,
            "duplicate_of_order_id": self.duplicate_of_order_id,
            "clarify": self.clarify,
            "missing_fields": list(self.missing_fields),
            "late": self.late,
            "unavailable_items": list(self.unavailable_items),
            "reply_hint": self.reply_hint,
        }
