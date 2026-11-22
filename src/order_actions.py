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
from .tally import TallyPushError, TallyPushFailed, TallyPushService
from .voucher import VoucherError, VoucherStore, prepare_voucher

SUPPORTED_ACTIONS = frozenset(
    {
        "approve",
        "reject",
        "dispatch",
        "prepare_voucher",
        "push_to_tally",
        "bill",
    }
)


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

    def __init__(
        self,
        core: OrderProcessingCore,
        store,
        voucher_storage: VoucherStore | None = None,
        tally_push: TallyPushService | None = None,
    ) -> None:
        self._core = core
        self._store = store
        self._voucher_storage = voucher_storage
        self._tally_push = tally_push

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
        action = str(arguments.get("action", ""))
        if action not in SUPPORTED_ACTIONS:
            raise UnsupportedOrderAction(f"unsupported order action {action!r}")

        if action == "prepare_voucher":
            if self._voucher_storage is None:
                raise VoucherError("voucher storage is not configured")
            await prepare_voucher(self._store, self._voucher_storage, order_id)
            updated = await self._store.get_order(order_id)
            if updated is None:
                raise RuntimeError(f"order {order_id} vanished after preparation")
        elif action == "push_to_tally":
            if self._tally_push is None:
                raise TallyPushError("Tally push is not configured")
            result = await self._tally_push.push(order_id, by=principal.id)
            if not result.ok:
                raise TallyPushFailed(result.error or "Tally push failed")
            updated = await self._store.get_order(order_id)
            if updated is None:
                raise RuntimeError(f"order {order_id} vanished after the Tally push")
        elif action == "dispatch":
            updated = await self._core.mark_dispatched(order_id, by=principal.id)
        elif action == "bill":
            updated = await self._core.mark_billed(order_id, by=principal.id)
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

    async def voucher_xml(
        self, arguments: Mapping[str, Any], principal: Principal
    ) -> str:
        """Return the generated XML for an order's prepared voucher.

        Authorization (approver role) is supplied by the REST router and the
        MCP gateway; this method only guards the artifact's availability.
        """
        order_id = str(arguments["order_id"])
        order = await self._store.get_order(order_id)
        if order is None:
            raise VoucherError(f"order {order_id} not found")
        if not order.voucher_id:
            raise VoucherError(f"order {order_id} has no prepared voucher")
        if self._voucher_storage is None:
            raise VoucherError("voucher storage is not configured")
        xml = await self._voucher_storage.read(order.voucher_id)
        if xml is None:
            raise VoucherError(f"voucher {order.voucher_id} is not available")
        return xml
