"""The dispatch-channel WhatsApp heads-up for late orders (issue #9).

When an order is approved after the daily cutoff, the dispatch channel is
notified instantly over WhatsApp so the yard can add it as an add-on to the
day's Loading List. The notification is recorded as an ``order_late`` Order
Event on the order's audit trail.
"""

from __future__ import annotations

from .orders import EVENT_ORDER_LATE, OrderEvent
from .store import OrderStore
from .whatsapp import WhatsAppSender


class LateOrderNotifier:
    """Sends an instant WhatsApp heads-up to the dispatch channel for late orders."""

    def __init__(self, store: OrderStore, sender: WhatsAppSender) -> None:
        self._store = store
        self._sender = sender

    async def on_order_late(self, order_id: str) -> None:
        """Notify the dispatch channel and record the event.

        Reads the ``dispatch_whatsapp_number`` from the store config. If the
        channel is not configured, silently does nothing — the order commit
        already succeeded and the notification is a convenience, not a
        correctness requirement.
        """
        config = await self._store.get_config()
        channel = str(config.get("dispatch_whatsapp_number", ""))
        if not channel:
            return
        self._sender.send(
            channel,
            f"Late order {order_id} — approved after the daily cutoff, "
            "added to the Loading List as an add-on.",
        )
        await self._store.append_order_event(
            OrderEvent(
                order_id=order_id,
                event_type=EVENT_ORDER_LATE,
                payload={"channel": channel},
            )
        )
