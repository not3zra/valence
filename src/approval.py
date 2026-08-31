"""The human-approval path over WhatsApp (issue #7).

When an order escalates, every allowlisted approver is told about it over
WhatsApp through the ``WhatsAppSender`` seam, and the notification is recorded
on the order's audit trail as an ``order_approval_requested`` Order Event — not
as a clarify-loop turn. Each approver's pending decision (approver phone ->
order id) is held in the store so their reply resolves to exactly one order;
the ``approve_order`` tool reads it back and clears it once decided. The store
and sender are the only seams this module touches.
"""

from __future__ import annotations

from .orders import EVENT_ORDER_APPROVAL_REQUESTED, OrderEvent
from .store import OrderStore
from .whatsapp import WhatsAppSender


_ESCALATION_LABELS = {
    "unknown_customer": "Unknown customer",
    "unverified_number": "Unverified number",
    "uncataloged_product": "Unknown product",
    "uncataloged_location": "Unknown delivery location",
    "missing_field": "Missing field",
    "low_confidence": "Low extraction confidence",
    "value_cap": "High order value",
    "quantity_anomaly": "Quantity anomaly",
    "rate_anomaly": "Rate anomaly",
}


def _build_approval_message(order) -> str:
    """Build a detailed approval notification for an order."""
    lines = [f"Order {order.order_id} needs your approval.\n"]

    # Items
    if order.items:
        item_lines = []
        for item in order.items:
            qty = f"{item.quantity:g}" if item.quantity else "?"
            unit = item.unit or "units"
            product = item.product or "unknown"
            item_lines.append(f"  - {qty} {unit} of {product}")
        lines.append("Items:")
        lines.extend(item_lines)

    # Delivery location
    if order.delivery_location:
        lines.append(f"Delivery: {order.delivery_location}")

    # Customer
    if order.customer:
        lines.append(f"Customer: {order.customer}")
    elif order.phone:
        lines.append(f"Phone: {order.phone}")

    # Estimated value
    if order.draft_value_inr:
        lines.append(f"Estimated value: INR {order.draft_value_inr:,.2f}")

    # Why it needs approval
    reasons = order.escalation_reasons or []
    if reasons:
        labels = [_ESCALATION_LABELS.get(r, r) for r in reasons]
        lines.append(f"Reason: {', '.join(labels)}")

    lines.append("\nReply CONFIRM to approve, or REJECT to reject.")
    return "\n".join(lines)


class ApprovalNotifier:
    """Tells every allowlisted approver an order needs a yes/no decision."""

    def __init__(self, store: OrderStore, sender: WhatsAppSender) -> None:
        self._store = store
        self._sender = sender

    async def on_order_escalated(self, order_id: str) -> None:
        """Register + announce the escalation to every allowlisted approver.

        Only allowlisted numbers are told about the order and only they get a
        pending-approval entry, so a non-allowlisted number's reply can never
        resolve to an order (the tool finds nothing pending for it).
        """
        order = await self._store.get_order(order_id)
        approvers = await self._store.get_approvers()
        message = _build_approval_message(order) if order else (
            f"Order {order_id} is waiting for your approval. "
            "Reply CONFIRM to approve, or REJECT to reject it."
        )
        for approver in approvers:
            await self._store.set_pending_approval(approver.phone, order_id)
            self._sender.send(approver.phone, message)
        await self._store.append_order_event(
            OrderEvent(
                order_id=order_id,
                event_type=EVENT_ORDER_APPROVAL_REQUESTED,
                payload={"notified": [approver.phone for approver in approvers]},
            )
        )
