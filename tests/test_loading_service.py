"""Loading List, Cutoff and stats service for the REST v1 Agent Interface (#62)."""

from __future__ import annotations

from datetime import date, datetime, timezone

from src.dispatch import LateOrderNotifier
from src.loading_service import LoadingService
from src.orders import Order, OrderItem, OrderStatus
from src.store import InMemoryOrderStore
from src.whatsapp import MockWhatsAppSender

DELIVERY_DAY = date(2026, 8, 14)
LATE = "2026-08-14T13:00:00+00:00"
BILLED = "2026-08-14T09:00:00+00:00"


def _order(
    order_id: str,
    *,
    location_id: str = "dl_peenya",
    updated_at: str = "2026-08-14T10:00:00+00:00",
    status: OrderStatus = OrderStatus.APPROVED,
) -> Order:
    return Order(
        order_id=order_id,
        phone="+919812345001",
        customer="ChemFab Industries",
        delivery_location="Peenya Industrial Area",
        delivery_location_id=location_id,
        items=[OrderItem(product="Sulfuric Acid", quantity=2000, unit="kg")],
        status=status,
        created_at=updated_at,
        updated_at=updated_at,
    )


async def test_render_groups_live_approved_orders_by_route_with_late_addon():
    store = InMemoryOrderStore()
    store.orders.extend(
        [
            _order("ord_west", location_id="dl_peenya"),
            _order("ord_east", location_id="dl_whitefield"),
            _order(
                "ord_late",
                updated_at="2026-08-14T13:00:00+00:00",
            ),
            _order("ord_pending", status=OrderStatus.PENDING_REVIEW),
        ]
    )

    result = await LoadingService(store).render(DELIVERY_DAY)

    assert {section["route_name"] for section in result["sections"]} == {
        "Bengaluru West",
        "Bengaluru East",
    }
    assert [entry["order_id"] for entry in result["late"]] == ["ord_late"]
    rendered = str(result).lower()
    assert "rate" not in rendered and "inr" not in rendered


async def test_stats_count_live_orders_with_a_business_timezone_cutoff():
    store = InMemoryOrderStore()
    store.orders.extend(
        [
            _order("ord_pending", status=OrderStatus.PENDING_REVIEW),
            _order("ord_early"),
            _order("ord_late", updated_at=LATE),
            _order("ord_billed", updated_at=BILLED, status=OrderStatus.BILLED),
            _order("ord_old", updated_at="2026-08-13T10:00:00+00:00"),
        ]
    )
    service = LoadingService(
        store, now=datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    )

    result = await service.stats()

    assert result == {
        "pending_escalations": 1,
        "approved_today": 2,
        "billed_today": 1,
        "late_orders": 1,
    }


async def test_cutoff_render_notifies_late_orders_only_once():
    store = InMemoryOrderStore()
    store.orders.append(_order("ord_late", updated_at=LATE))
    sender = MockWhatsAppSender()
    service = LoadingService(store, LateOrderNotifier(store, sender))

    first = await service.cutoff_render(DELIVERY_DAY)
    await service.cutoff_render(DELIVERY_DAY)

    assert [entry["order_id"] for entry in first["late"]] == ["ord_late"]
    assert len(sender.sent) == 1
    assert sender.sent[0][0] == "+919845000003"
    events = await store.list_order_events("ord_late")
    assert [event.event_type for event in events] == ["order_late"]
