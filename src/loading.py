"""The Loading List renderer (issue #9).

The dispatch-facing document for a delivery day: a rendered view of live
approved orders — never a static snapshot. Approved orders group into per-route
sections through their delivery location's route; an unrouted bucket holds
orders whose location resolves to no route; orders approved on the delivery day
after the day's cutoff land in a late add-on section (the same live definition
the review stat bar uses). The list is price-free: rates and draft estimates
never appear, in the data or the HTML. Built as pure functions over the live
order set so the web view, the ADK tool, and the Cutoff job render the exact
same list.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from html import escape
from zoneinfo import ZoneInfo

from .orders import Order, OrderItem, OrderStatus, iso_to_dt
from .seed_data import DeliveryLocation, Route
from .store import OrderStore
from .ui import DESIGN_TOKENS, COMPONENT_CSS, login_page_shell

DEFAULT_CUTOFF_TIME: time = time(17, 30)

DEFAULT_BUSINESS_TZ: str = "Asia/Kolkata"

PASSCODE_COOKIE = "valence_loading"


def parse_cutoff(config: dict) -> time:
    """Read the configured daily cutoff time, defaulting when absent."""
    value = config.get("cutoff_time")
    if not value:
        return DEFAULT_CUTOFF_TIME
    try:
        hour, minute = (int(part) for part in str(value).split(":"))
    except (TypeError, ValueError):
        return DEFAULT_CUTOFF_TIME
    return time(hour, minute)


def parse_business_tz(config: dict) -> ZoneInfo:
    """Read the configured business timezone, defaulting to IST.

    The cutoff and the delivery day are business-time concepts (ADR-0002); the
    scheduler and the store timestamps both resolve against this zone so the
    cutoff comparison and the render agree no matter where the container runs.
    """
    name = str(config.get("business_timezone") or DEFAULT_BUSINESS_TZ)
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo(DEFAULT_BUSINESS_TZ)


def is_late_for(
    order: Order,
    delivery_day: date,
    cutoff_time: time,
    business_tz: ZoneInfo,
) -> bool:
    """True when ``order`` was approved on ``delivery_day`` after its cutoff.

    The approval moment is the order's ``updated_at`` stamp — set at intake for
    an auto-approved order, and at decision time for a human approval (issue
    #7). Only an approved order can be late; anything else (pending, dispatched,
    billed, rejected) is never. ``updated_at`` is stored in UTC; both the
    delivery-day and cutoff comparisons happen in ``business_tz`` so an order
    approved just after 17:30 IST reads as late even though its UTC clock shows
    an earlier hour (issue #9).
    """
    if order.status is not OrderStatus.APPROVED:
        return False
    approved_at = iso_to_dt(order.updated_at).astimezone(business_tz)
    return approved_at.date() == delivery_day and approved_at.time() > cutoff_time


def _item_no_price(item: OrderItem) -> dict:
    """Serialize one line price-free: product, quantity, unit — never a rate."""
    return {"product": item.product, "quantity": item.quantity, "unit": item.unit}


@dataclass(frozen=True)
class LoadingEntry:
    order_id: str
    customer: str | None
    delivery_location: str | None
    items: list[OrderItem]
    created_at: str
    route_name: str | None = None

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "customer": self.customer,
            "delivery_location": self.delivery_location,
            "items": [_item_no_price(item) for item in self.items],
            "created_at": self.created_at,
            "route_name": self.route_name,
        }


@dataclass(frozen=True)
class RouteSection:
    route_id: str
    route_name: str
    entries: list[LoadingEntry]

    def to_dict(self) -> dict:
        return {
            "route_id": self.route_id,
            "route_name": self.route_name,
            "entries": [entry.to_dict() for entry in self.entries],
        }


@dataclass(frozen=True)
class LoadingList:
    delivery_day: str
    cutoff_time: str
    sections: list[RouteSection]
    unrouted: list[LoadingEntry]
    late: list[LoadingEntry]

    def to_dict(self) -> dict:
        return {
            "delivery_day": self.delivery_day,
            "cutoff_time": self.cutoff_time,
            "sections": [section.to_dict() for section in self.sections],
            "unrouted": [entry.to_dict() for entry in self.unrouted],
            "late": [entry.to_dict() for entry in self.late],
        }


def build_loading_list(
    orders: list[Order],
    delivery_locations: list[DeliveryLocation],
    routes: list[Route],
    *,
    delivery_day: date,
    cutoff_time: time,
    business_tz: ZoneInfo,
) -> LoadingList:
    """Group live approved orders into the delivery day's Loading List.

    Only ``approved`` orders are listed; dispatched, billed, rejected, and
    pending-rejected orders never appear. Each approved order resolves to a
    route through its delivery location; a location that resolves to no seeded
    route lands in the unrouted bucket. An order approved on the delivery day
    after the cutoff is a late add-on, not part of the main sections.
    """
    location_by_id = {location.id: location for location in delivery_locations}
    route_name = {route.id: route.name for route in routes}

    late: list[LoadingEntry] = []
    by_route: dict[str, list[LoadingEntry]] = {}
    unrouted: list[LoadingEntry] = []

    for order in orders:
        if order.status is not OrderStatus.APPROVED:
            continue
        location = location_by_id.get(order.delivery_location_id or "")
        route_id = location.route_id if location is not None else None
        route = route_name.get(route_id) if route_id is not None else None
        entry = LoadingEntry(
            order_id=order.order_id or "",
            customer=order.customer,
            delivery_location=order.delivery_location,
            items=list(order.items),
            created_at=order.created_at,
            route_name=route,
        )
        if is_late_for(order, delivery_day, cutoff_time, business_tz):
            late.append(entry)
        elif route is None:
            unrouted.append(entry)
        else:
            assert route_id is not None
            by_route.setdefault(route_id, []).append(entry)

    def _sort(entries: list[LoadingEntry]) -> list[LoadingEntry]:
        return sorted(entries, key=lambda e: (e.delivery_location or "", e.created_at))

    sections = [
        RouteSection(route_id=rid, route_name=route_name[rid], entries=_sort(entries))
        for rid, entries in sorted(by_route.items(), key=lambda kv: route_name[kv[0]])
    ]
    return LoadingList(
        delivery_day=delivery_day.isoformat(),
        cutoff_time=cutoff_time.isoformat(),
        sections=sections,
        unrouted=_sort(unrouted),
        late=_sort(late),
    )


async def load_loading_list(
    store: OrderStore,
    *,
    delivery_day: date | None = None,
    cutoff_time: time | None = None,
    business_tz: ZoneInfo | None = None,
) -> LoadingList:
    """Render a delivery day's Loading List from live store state.

    The one seam the web view, the ADK tool, and the Cutoff job all share: read
    the live order set plus the route/delivery-location masters and build the
    list. When omitted, ``cutoff_time`` and ``business_tz`` come from the store
    config and ``delivery_day`` defaults to today in the business timezone —
    so a bare call from any of the three callers renders the same day's list.
    """
    config = await store.get_config()
    cutoff = cutoff_time or parse_cutoff(config)
    tz = business_tz or parse_business_tz(config)
    day = delivery_day or datetime.now(tz).date()
    orders = await store.list_approved_orders()
    locations = await store.get_delivery_locations()
    routes = await store.get_routes()
    return build_loading_list(
        orders,
        locations,
        routes,
        delivery_day=day,
        cutoff_time=cutoff,
        business_tz=tz,
    )


def _escaped(value: object) -> str:
    return escape("" if value is None else str(value))


def _items_text(entry: LoadingEntry) -> str:
    parts = [
        f"{item.quantity:g} {item.unit} {item.product}".strip()
        for item in entry.items
    ]
    return ", ".join(parts) if parts else "\u2014"


def _entry_table(entries: list[LoadingEntry], *, with_action: bool) -> str:
    rows = []
    for entry in entries:
        action = (
            "<td>"
            f"<form method='post' "
            f"action='/loading/orders/{_escaped(entry.order_id)}/dispatch'>"
            "<button class='dispatch' type='submit'>Mark dispatched</button>"
            "</form></td>"
        ) if with_action else ""
        rows.append(
            f"<tr><td style='font-weight:600'>{_escaped(entry.order_id)}</td>"
            f"<td>{_escaped(entry.customer or '\u2014')}</td>"
            f"<td>{_escaped(entry.delivery_location or '\u2014')}</td>"
            f"<td>{_escaped(_items_text(entry))}</td>{action}</tr>"
        )
    body = "".join(rows)
    if not body:
        body = "<tr><td colspan='5' class='sub'>None.</td></tr>"
    return (
        f"<table><thead><tr><th>Order</th><th>Customer</th>"
        f"<th>Delivery location</th><th>Items</th>"
        f"{'<th></th>' if with_action else ''}</tr></thead>"
        f"<tbody>{body}</tbody></table>"
    )


def render_loading_list_html(loading_list: LoadingList) -> str:
    """A printable, price-free HTML page for the delivery day's Loading List."""
    sections = "".join(
        f"<div class='card'>"
        f"<h2 style='margin-top:0'>"
        f"<span class='badge n'>{_escaped(section.route_name)}</span></h2>"
        f"{_entry_table(section.entries, with_action=True)}</div>"
        for section in loading_list.sections
    )
    if not sections:
        sections = (
            "<div class='card empty-state'>"
            "<p>No approved orders to load for this day.</p></div>"
        )

    late_section = ""
    if loading_list.late:
        late_section = (
            f"<div class='card'>"
            f"<h2 style='margin-top:0'>"
            f"<span class='badge b'>Late add-ons \u2014 approved after cutoff</span>"
            f"</h2>"
            f"{_entry_table(loading_list.late, with_action=True)}</div>"
        )

    unrouted_section = ""
    if loading_list.unrouted:
        unrouted_section = (
            f"<div class='card'>"
            f"<h2 style='margin-top:0'>Unrouted</h2>"
            f"{_entry_table(loading_list.unrouted, with_action=True)}</div>"
        )

    body = (
        f"<h1>Loading List \u2014 {_escaped(loading_list.delivery_day)}</h1>"
        f"<p class='sub'>Cutoff {_escaped(loading_list.cutoff_time)} \xb7 "
        f"live approved orders from the Order Processing Core \xb7 price-free.</p>"
        f"<p class='no-print' style='margin-bottom:var(--space-5)'>"
        f"<button onclick='window.print()'>Print</button></p>"
        f"{late_section}{unrouted_section}"
        f"<h2>Routes</h2>{sections}"
    )
    return (
        "<!doctype html><html lang='en'><head>"
        "<meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>Loading List {_escaped(loading_list.delivery_day)} \u2014 Valence</title>"
        f"<style>{DESIGN_TOKENS}{COMPONENT_CSS}</style>"
        "</head><body>"
        "<header>"
        "<a href='/' class='brand'>Valence</a>"
        "<nav><a href='/loading'>Loading list</a></nav>"
        "<form method='post' action='/loading/logout'>"
        "<button class='btn-ghost' type='submit' "
        "style='color:#94a3b8;border-color:transparent'>"
        "Log out</button></form>"
        "</header>"
        f"<div class='wrap'>{body}</div>"
        "</body></html>"
    )


def loading_login_page(error: str | None = None) -> str:
    """Passcode login form gating the Loading List web view (issue #9)."""
    return login_page_shell(
        "Log in",
        "Valence Loading",
        "Enter the demo passcode to open the day's Loading List.",
        "/loading/login",
        error=error,
    )
