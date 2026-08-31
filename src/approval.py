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

import logging

from .orders import EVENT_ORDER_APPROVAL_REQUESTED, OrderEvent
from .store import OrderStore
from .whatsapp import WhatsAppSender


class ApprovalNotifier:
    """Tells every allowlisted approver an order needs a yes/no decision."""

    def __init__(
        self,
        store: OrderStore,
        sender: WhatsAppSender,
        *,
        service_url: str = "",
    ) -> None:
        self._store = store
        self._sender = sender
        self._service_url = service_url

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
        for approver in approvers:
            await self._store.set_pending_approval(approver.phone, order_id)
            # Check if this approver already has other pending orders.
            pending_ids = await self._store.get_pending_approvals_list(
                approver.phone
            )
            if len(pending_ids) > 1:
                # Multiple pending — send a list summary with review links.
                msg = await _build_list_pending_message(
                    self._store,
                    pending_ids,
                    service_url=self._service_url,
                )
            else:
                msg = _build_approval_message(
                    order_id,
                    phone=phone,
                    customer=customer,
                    delivery_location=delivery_location,
                    items=items,
                    draft_value_inr=draft_value_inr,
                    escalation_reasons=escalation_reasons,
                    service_url=self._service_url,
                )
            self._sender.send(approver.phone, msg)
        await self._store.append_order_event(
            OrderEvent(
                order_id=order_id,
                event_type=EVENT_ORDER_APPROVAL_REQUESTED,
                payload={"notified": [approver.phone for approver in approvers]},
            )
        )

    async def on_order_decided(
        self,
        order_id: str,
        *,
        approved: bool,
        customer_phone: str,
        customer_name: str | None = None,
    ) -> None:
        """Notify the customer that their order was approved or rejected."""
        if not customer_phone:
            logging.warning(
                "[Approval] skipping customer notification for %s: "
                "no customer phone",
                order_id,
            )
            return
        if approved:
            msg = (
                f"Your order {order_id} has been *approved*. "
                "Please contact the staff for any queries regarding the order."
            )
        else:
            msg = (
                f"Your order {order_id} has been *rejected*. "
                "Please contact the staff for any queries regarding the order."
            )
        logging.info(
            "[Approval] notifying customer %s about order %s (approved=%s)",
            customer_phone,
            order_id,
            approved,
        )
        self._sender.send(customer_phone, msg)


def _build_approval_message(
    order_id: str,
    *,
    phone: str = "",
    customer: str | None = None,
    delivery_location: str | None = None,
    items: list[dict] | None = None,
    draft_value_inr: float = 0.0,
    escalation_reasons: list[str] | None = None,
    service_url: str = "",
) -> str:
    """Build a rich approval message with order context."""
    lines = [f"Order {order_id} needs your approval."]
    if items:
        lines.append("")
        lines.append("Items:")
        for item in items:
            product = item.get("product", item.get("product_name", "?"))
            qty = item.get("quantity", "?")
            unit = item.get("unit", "")
            lines.append(f"  - {qty} {unit} of {product}")
    if delivery_location:
        lines.append(f"Delivery: {delivery_location}")
    if phone:
        lines.append(f"Phone: {phone}")
    if draft_value_inr:
        lines.append(f"Estimated value: INR {draft_value_inr:,.2f}")
    if escalation_reasons:
        reasons = ", ".join(
            r.replace("_", " ").title() for r in escalation_reasons
        )
        lines.append(f"Reason: {reasons}")
    if service_url:
        lines.append(
            f"\nReview: {service_url.rstrip('/')}/review/orders/{order_id}"
        )
    lines.append("\nReply CONFIRM to approve, or REJECT to reject.")
    return "\n".join(lines)


async def _build_list_pending_message(
    store: OrderStore,
    order_ids: list[str],
    *,
    service_url: str = "",
) -> str:
    """Build a message listing all pending orders with review links."""
    lines = ["You have multiple pending orders awaiting your decision:", ""]
    for i, oid in enumerate(order_ids, 1):
        order = await store.get_order(oid)
        if order is None:
            lines.append(f"{i}. *Order ID: {oid}* (unknown)")
            continue
        location = order.delivery_location or "Unknown"
        items_text = ", ".join(
            f"{item.quantity} {item.unit} of {item.product}" for item in order.items
        )
        value = f"₹{order.draft_value_inr:,.2f}" if order.draft_value_inr else "N/A"
        lines.append(f"{i}. *Order ID: {oid}*")
        lines.append(f"   - *Location:* {location}")
        lines.append(f"   - *Items:* {items_text}")
        lines.append(f"   - *Value:* {value}")
        if order.escalation_reasons:
            reasons = ", ".join(
                r.replace("_", " ").title() for r in order.escalation_reasons
            )
            lines.append(f"   - *Reason:* {reasons}")
        if service_url:
            lines.append(
                f"   - *Review:* {service_url.rstrip('/')}/review/orders/{oid}"
            )
        lines.append("")
    lines.append(
        "Please reply with the order ID you want to approve or reject "
        "(e.g. CONFIRM ord_xxx or REJECT ord_xxx)."
    )
    return "\n".join(lines)
