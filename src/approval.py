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


class ApprovalNotifier:
    """Tells every allowlisted approver an order needs a yes/no decision."""

    def __init__(self, store: OrderStore, sender: WhatsAppSender) -> None:
        self._store = store
        self._sender = sender

    async def on_order_escalated(
        self,
        order_id: str,
        *,
        phone: str = "",
        customer: str | None = None,
        delivery_location: str | None = None,
        items: list[dict] | None = None,
        draft_value_inr: float = 0.0,
        escalation_reasons: list[str] | None = None,
    ) -> None:
        """Register + announce the escalation to every allowlisted approver.

        Only allowlisted numbers are told about the order and only they get a
        pending-approval entry, so a non-allowlisted number's reply can never
        resolve to an order (the tool finds nothing pending for it).
        """
        approvers = await self._store.get_approvers()
        msg = _build_approval_message(
            order_id,
            phone=phone,
            customer=customer,
            delivery_location=delivery_location,
            items=items,
            draft_value_inr=draft_value_inr,
            escalation_reasons=escalation_reasons,
        )
        for approver in approvers:
            await self._store.set_pending_approval(approver.phone, order_id)
            self._sender.send(approver.phone, msg)
        await self._store.append_order_event(
            OrderEvent(
                order_id=order_id,
                event_type=EVENT_ORDER_APPROVAL_REQUESTED,
                payload={"notified": [approver.phone for approver in approvers]},
            )
        )


def _build_approval_message(
    order_id: str,
    *,
    phone: str = "",
    customer: str | None = None,
    delivery_location: str | None = None,
    items: list[dict] | None = None,
    draft_value_inr: float = 0.0,
    escalation_reasons: list[str] | None = None,
) -> str:
    """Build a rich approval message with order context."""
    lines = [f"*New order awaiting approval*  ({order_id})"]
    if phone:
        lines.append(f"From: {phone}")
    if customer:
        lines.append(f"Customer: {customer}")
    if delivery_location:
        lines.append(f"Delivery: {delivery_location}")
    if items:
        lines.append("")
        for item in items:
            product = item.get("product", item.get("product_name", "?"))
            qty = item.get("quantity", "?")
            unit = item.get("unit", "")
            rate = item.get("rate_inr")
            part = f"  - {qty} {unit} {product}".strip()
            if rate:
                part += f" @ ₹{rate}"
            lines.append(part)
    if draft_value_inr:
        lines.append(f"\nEstimated value: ₹{draft_value_inr:,.2f}")
    if escalation_reasons:
        reasons = ", ".join(r.replace("_", " ") for r in escalation_reasons)
        lines.append(f"Reason: {reasons}")
    lines.append("\nReply CONFIRM to approve, or REJECT to reject.")
    return "\n".join(lines)
