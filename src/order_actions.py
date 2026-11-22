"""Order editing and confirmed approval actions shared by REST and MCP (#60).

The Agent Interface has one implementation for each privileged order operation.
REST routes and MCP tool handlers both call :class:`OrderActionService`, while
authorization (roles, approver identity, ``confirm``) stays in ``AuthService``
and is enforced by the REST router and the MCP gateway before this service runs.
Every operation goes through ``OrderProcessingCore`` so it lands on the shared
append-only Order Event audit trail.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from .auth import Principal
from .core import OrderProcessingCore
from .orders import Order, OrderEvent

SUPPORTED_ACTIONS = frozenset({"approve", "reject", "dispatch"})


class UnsupportedOrderAction(ValueError):
    """An action the Agent Interface does not implement."""


def order_summary(order: Order) -> dict[str, Any]:
    """The list-view projection of an order."""
    return {
        "order_id": order.order_id,
        "status": order.status.value,
        "customer": order.customer,
        "phone": order.phone,
        "source_channel": order.source_channel,
        "draft_value_inr": order.draft_value_inr,
        "escalation_reasons": list(order.escalation_reasons),
        "voucher_id": order.voucher_id,
        "created_at": order.created_at,
    }


def order_detail(order: Order, events: list[OrderEvent]) -> dict[str, Any]:
    """The detail-view projection of an order, including its event timeline."""
    return {
        **order_summary(order),
        "customer_id": order.customer_id,
        "delivery_location": order.delivery_location,
        "delivery_location_id": order.delivery_location_id,
        "items": [asdict(item) for item in order.items],
        "confidence": order.confidence,
        "updated_at": order.updated_at,
        "voucher": {
            "prepared": order.voucher_id is not None,
            "voucher_id": order.voucher_id,
        },
        "events": [
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "payload": dict(event.payload),
                "created_at": event.created_at,
            }
            for event in sorted(events, key=lambda event: event.created_at)
        ],
    }


class OrderActionService:
    """The single service layer for editing and deciding an order."""

    def __init__(self, core: OrderProcessingCore, store) -> None:
        self._core = core
        self._store = store

    async def edit(
        self, arguments: Mapping[str, Any], principal: Principal
    ) -> dict[str, Any]:
        """Apply an approver's field corrections and return the updated detail."""
        order_id = str(arguments["order_id"])
        changes = dict(arguments.get("changes", {}))
        order = await self._core.edit_order(order_id, changes=changes, by=principal.id)
        events = await self._store.list_order_events(order_id)
        return order_detail(order, events)

    async def act(
        self, arguments: Mapping[str, Any], principal: Principal
    ) -> dict[str, Any]:
        """Apply a confirmed decision and return the resulting order state."""
        order_id = str(arguments["order_id"])
        action = str(arguments["action"])
        if action not in SUPPORTED_ACTIONS:
            raise UnsupportedOrderAction(f"unsupported order action {action!r}")

        if action == "dispatch":
            updated = await self._core.mark_dispatched(order_id, by=principal.id)
        else:
            await self._core.approve_order(
                order_id,
                approved=action == "approve",
                by_phone=principal.phone or "",
            )
            updated = await self._store.get_order(order_id)
            if updated is None:
                raise RuntimeError(f"order {order_id} vanished after a decision")

        events = await self._store.list_order_events(order_id)
        return {
            "order_id": updated.order_id or order_id,
            "action": action,
            "status": updated.status.value,
            "order": order_detail(updated, events),
        }
