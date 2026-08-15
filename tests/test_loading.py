"""The Loading List renderer (issue #9).

The dispatch-facing document for a delivery day: a rendered view of live
approved orders — grouped per route with an unrouted bucket for orders whose
delivery location resolves to no route, and a late add-on section — carrying no
prices. Built as pure functions over the live order set so the web view and the
ADK tool render the exact same list.
"""

from __future__ import annotations

from datetime import date, time
from zoneinfo import ZoneInfo

from src.loading import (
    DEFAULT_CUTOFF_TIME,
    build_loading_list,
    is_late_for,
    parse_business_tz,
    parse_cutoff,
    render_loading_list_html,
)
from src.orders import Order, OrderItem, OrderStatus
from src.seed_data import DELIVERY_LOCATIONS, ROUTES

CUTOFF = time(17, 30)
DAY = date(2026, 8, 14)
TZ = ZoneInfo("Asia/Kolkata")


def _approved(
    order_id: str = "ord_x",
    *,
    location_id: str = "dl_peenya",
    when: str = "2026-08-14T10:00:00+00:00",
    status: OrderStatus = OrderStatus.APPROVED,
    phone: str = "+919812345001",
) -> Order:
    return Order(
        order_id=order_id,
        phone=phone,
        customer="ChemFab Industries",
        delivery_location="Peenya Industrial Area",
        delivery_location_id=location_id,
        items=[OrderItem(product="Sulfuric Acid", quantity=2000, unit="kg")],
        status=status,
        created_at=when,
        updated_at=when,
    )


def _render(*orders: Order):
    return build_loading_list(
        list(orders),
        DELIVERY_LOCATIONS,
        ROUTES,
        delivery_day=DAY,
        cutoff_time=CUTOFF,
        business_tz=TZ,
    )


def test_approved_orders_grouped_into_route_sections():
    west = _approved(order_id="ord_west", location_id="dl_peenya")
    east = _approved(order_id="ord_east", location_id="dl_whitefield")

    result = _render(west, east)

    names = {section.route_name for section in result.sections}
    assert names == {"Bengaluru West", "Bengaluru East"}
    by_name = {section.route_name: section for section in result.sections}
    assert by_name["Bengaluru West"].entries[0].order_id == "ord_west"
    assert by_name["Bengaluru East"].entries[0].order_id == "ord_east"


def test_orders_without_a_route_land_in_the_unrouted_bucket():
    unrouted = _approved(order_id="ord_loose", location_id="dl_unknown")

    result = _render(unrouted)

    assert result.sections == []
    assert [entry.order_id for entry in result.unrouted] == ["ord_loose"]


def test_only_approved_orders_are_listed():
    pending = _approved(
        order_id="ord_pending", status=OrderStatus.PENDING_REVIEW
    )
    dispatched = _approved(order_id="ord_disp", status=OrderStatus.DISPATCHED)
    billed = _approved(order_id="ord_billed", status=OrderStatus.BILLED)
    rejected = _approved(order_id="ord_rejected", status=OrderStatus.REJECTED)

    result = _render(pending, dispatched, billed, rejected)

    assert result.sections == []
    assert result.unrouted == []
    assert result.late == []


def test_order_approved_after_cutoff_is_a_late_addon():
    early = _approved(order_id="ord_early", when="2026-08-14T10:00:00+00:00")
    late = _approved(order_id="ord_late", when="2026-08-14T18:00:00+00:00")

    result = _render(early, late)

    assert [entry.order_id for entry in result.late] == ["ord_late"]
    main_ids = [
        entry.order_id
        for section in result.sections
        for entry in section.entries
    ]
    assert main_ids == ["ord_early"]


def test_order_approved_before_cutoff_stays_in_the_main_section():
    early = _approved(order_id="ord_early", when="2026-08-14T10:00:00+00:00")

    result = _render(early)

    assert result.late == []
    assert any(
        entry.order_id == "ord_early"
        for section in result.sections
        for entry in section.entries
    )


def test_the_list_carries_no_prices():
    order = _approved(order_id="ord_x", when="2026-08-14T18:00:00+00:00")

    result = _render(order)

    serialized = str(result.to_dict()).lower()
    assert "inr" not in serialized
    assert "rate" not in serialized


def test_is_late_for_after_cutoff_on_the_delivery_day():
    order = _approved(when="2026-08-14T18:00:00+00:00")
    assert is_late_for(order, DAY, CUTOFF, TZ) is True


def test_is_late_for_before_cutoff():
    order = _approved(when="2026-08-14T10:00:00+00:00")
    assert is_late_for(order, DAY, CUTOFF, TZ) is False


def test_is_late_for_other_day_is_not_late():
    order = _approved(when="2026-08-13T18:00:00+00:00")
    assert is_late_for(order, DAY, CUTOFF, TZ) is False


def test_is_late_for_unapproved_order_is_never_late():
    order = _approved(when="2026-08-14T18:00:00+00:00")
    order.status = OrderStatus.PENDING_REVIEW
    assert is_late_for(order, DAY, CUTOFF, TZ) is False


def test_is_late_for_compares_in_the_business_timezone():
    # 17:45 UTC = 23:15 IST, still the same delivery day but after the 17:30
    # cutoff in business time — the UTC clock alone would read it as on-time
    # (issue #9 cutoff skew).
    order = _approved(when="2026-08-14T17:45:00+00:00")
    assert is_late_for(order, DAY, CUTOFF, TZ) is True
    # 12:45 UTC = 18:15 IST — before the 17:30 cutoff in UTC, but past it in
    # business time, so it is late.
    order = _approved(when="2026-08-14T12:45:00+00:00")
    assert is_late_for(order, DAY, CUTOFF, TZ) is True
    # 11:00 UTC = 16:30 IST — before the cutoff in business time.
    order = _approved(when="2026-08-14T11:00:00+00:00")
    assert is_late_for(order, DAY, CUTOFF, TZ) is False


def test_is_late_for_crosses_the_delivery_day_in_business_time():
    # 20:00 UTC = 01:30 IST the NEXT day: the approval belongs to the Aug 15
    # delivery day, not Aug 14, and lands well before Aug 15's cutoff — so it
    # is neither late for this day nor for the day it actually belongs to.
    order = _approved(when="2026-08-14T20:00:00+00:00")
    assert is_late_for(order, DAY, CUTOFF, TZ) is False
    assert is_late_for(order, date(2026, 8, 15), CUTOFF, TZ) is False


def test_parse_business_tz_defaults_when_missing_or_invalid():
    assert parse_business_tz({}) == ZoneInfo("Asia/Kolkata")
    assert parse_business_tz({"business_timezone": "America/New_York"}) == ZoneInfo(
        "America/New_York"
    )
    assert parse_business_tz({"business_timezone": "Not/AZone"}) == ZoneInfo(
        "Asia/Kolkata"
    )


def test_parse_cutoff_defaults_when_missing():
    assert parse_cutoff({}) == DEFAULT_CUTOFF_TIME
    assert parse_cutoff({"cutoff_time": "16:15"}) == time(16, 15)


def test_rendered_html_is_printable_and_price_free():
    late = _approved(order_id="ord_late", when="2026-08-14T18:00:00+00:00")
    early = _approved(order_id="ord_early", when="2026-08-14T10:00:00+00:00")

    html = render_loading_list_html(_render(late, early))

    assert "@media print" in html
    assert "INR" not in html
    assert "ord_late" in html and "ord_early" in html
    assert "Late add-ons" in html
    assert "Bengaluru West" in html
