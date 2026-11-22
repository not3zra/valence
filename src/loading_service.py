"""Loading List, Cutoff and live stats service for the REST v1 Agent Interface.

Issue #62: the Agent Interface exposes the same live Loading List renderer and
stat computation the web view and the ADK tool already share. The service is a
thin application layer over :func:`src.loading.load_loading_list` and
:func:`src.review.compute_stats`; it imports no agent-framework types and reads
only through the ``OrderStore`` protocol. Late-order heads-ups are dispatched
through an injected sink so the cutoff render can notify the yard without the
service knowing about WhatsApp.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol

from .loading import load_loading_list, parse_business_tz, parse_cutoff
from .orders import EVENT_ORDER_LATE
from .review import compute_stats
from .store import OrderStore


class LateOrderSink(Protocol):
    """The dispatch heads-up seam used by the cutoff render."""

    async def on_order_late(self, order_id: str) -> None: ...


class LoadingService:
    """Render live Loading Lists, live stats, and the Cutoff batch for one Instance."""

    def __init__(
        self,
        store: OrderStore,
        notifier: LateOrderSink | None = None,
        now: datetime | None = None,
    ) -> None:
        self._store = store
        self._notifier = notifier
        self._now = now

    async def render_loading_list(self, delivery_day: date | None = None) -> dict:
        """Render the delivery day's Loading List from live approved orders."""
        loading = await load_loading_list(self._store, delivery_day=delivery_day)
        return loading.to_dict()

    async def stats(self) -> dict[str, int]:
        """Live pending / approved / billed / late counts from the order set."""
        config = await self._store.get_config()
        return compute_stats(
            await self._store.list_all_orders(),
            now=self._now,
            cutoff_time=parse_cutoff(config),
            business_tz=parse_business_tz(config),
        )

    async def render_cutoff(self, delivery_day: date | None = None) -> dict:
        """Render the day's Loading List and raise a heads-up for each late order.

        The admin REST trigger and the secret-gated scheduler endpoint both call
        this seam. Notification is idempotent: an order that already carries an
        ``order_late`` event (the approval path notifies too) is not sent again.
        """
        loading = await load_loading_list(self._store, delivery_day=delivery_day)
        if self._notifier is not None:
            for entry in loading.late:
                events = await self._store.list_order_events(entry.order_id)
                if any(event.event_type == EVENT_ORDER_LATE for event in events):
                    continue
                await self._notifier.on_order_late(entry.order_id)
        return loading.to_dict()
