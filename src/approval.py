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

    async def on_order_escalated(self, order_id: str) -> None:
        """Register + announce the escalation to every allowlisted approver.

        Only allowlisted numbers are told about the order and only they get a
        pending-approval entry, so a non-allowlisted number's reply can never
        resolve to an order (the tool finds nothing pending for it).
        """
        approvers = await self._store.get_approvers()
        for approver in approvers:
            await self._store.set_pending_approval(approver.phone, order_id)
            self._sender.send(
                approver.phone,
                f"Order {order_id} is waiting for your approval. "
                "Reply CONFIRM to approve, or REJECT to reject it.",
            )
        await self._store.append_order_event(
            OrderEvent(
                order_id=order_id,
                event_type=EVENT_ORDER_APPROVAL_REQUESTED,
                payload={"notified": [approver.phone for approver in approvers]},
            )
        )
