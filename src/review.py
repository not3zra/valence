"""The review web view (issue #6): the human interface for escalated orders.

Server-rendered HTML with no frontend build, served from the FastAPI web layer
behind a single demo passcode read from the store config. The escalation queue,
an order detail with its Order Event timeline, search, and a live stat bar are
all rendered here as pure functions over the live order set; the web layer owns
the routes, the passcode gate, and the ``approve_order_web`` calls that keep web
and WhatsApp decisions in sync through the same Order Processing Core.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from html import escape

from .orders import Order, OrderEvent, OrderItem, OrderStatus
from .ui import DESIGN_TOKENS, COMPONENT_CSS, page_shell, login_page_shell

PASSCODE_COOKIE = "valence_review"

DEFAULT_CUTOFF_TIME: time = time(17, 30)

REASON_LABELS: dict[str, str] = {
    "missing_field": "Missing field",
    "unknown_customer": "Unknown customer",
    "unverified_number": "Unverified number",
    "uncataloged_product": "Uncataloged product",
    "low_confidence": "Low confidence",
    "over_value_cap": "Over value cap",
    "anomaly": "Anomaly",
}

STATUS_LABELS: dict[str, str] = {
    OrderStatus.PENDING_REVIEW.value: "Pending review",
    OrderStatus.APPROVED.value: "Approved",
    OrderStatus.DISPATCHED.value: "Dispatched",
    OrderStatus.BILLED.value: "Billed",
    OrderStatus.REJECTED.value: "Rejected",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_to_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _local_day(value: str) -> date:
    return _iso_to_dt(value).date()


def compute_stats(
    orders: list[Order],
    *,
    now: datetime | None = None,
    cutoff_time: time = DEFAULT_CUTOFF_TIME,
) -> dict[str, int]:
    """Live stat-bar counts over the order set (issue #6).

    ``pending_escalations`` counts orders still sitting in ``pending_review``.
    ``approved_today`` / ``billed_today`` count orders whose status changed to
    approved / billed today (the status change stamps ``updated_at``). A
    ``late_orders`` count is an approved order approved today after the
    configured daily cutoff — the same live definition the Loading List renderer
    will use (issue #9). ``now`` pins today for deterministic tests.
    """
    today = (now if now is not None else _now()).date()
    pending = approved = billed = late = 0
    for order in orders:
        if order.status is OrderStatus.PENDING_REVIEW:
            pending += 1
        elif order.status is OrderStatus.APPROVED:
            updated = _iso_to_dt(order.updated_at)
            if updated.date() == today:
                approved += 1
                if updated.time() > cutoff_time:
                    late += 1
        elif order.status is OrderStatus.BILLED:
            if _local_day(order.updated_at) == today:
                billed += 1
    return {
        "pending_escalations": pending,
        "approved_today": approved,
        "billed_today": billed,
        "late_orders": late,
    }


def _escaped(value: object) -> str:
    return escape("" if value is None else str(value))


def _reason_badges(reasons: list[str]) -> str:
    if not reasons:
        return ""
    spans = "".join(
        f'<span class="badge b">{escape(REASON_LABELS.get(r, r))}</span>'
        for r in reasons
    )
    return f'<div class="badges">{spans}</div>'


def _items_summary(order: Order) -> str:
    parts = [
        f"{item.quantity:g} {item.unit} {item.product}".strip()
        for item in order.items
    ]
    return escape(", ".join(parts)) if parts else "\u2014"


def page(title: str, body: str) -> str:
    """Wrap ``body`` in the shared review shell (header, nav, logout, CSS)."""
    return page_shell(title, body)


def login_page(error: str | None = None) -> str:
    """Passcode login form; ``error`` renders a banner above the field."""
    return login_page_shell(
        "Log in",
        "Valence Review",
        "Enter the demo passcode to open the escalation queue.",
        "/review/login",
        error=error,
    )


def stat_bar(stats: dict[str, int]) -> str:
    """Stat-bar cards with a JS poll that refreshes them every 10s in place."""
    rows = [
        ("pending", "Pending escalations", stats["pending_escalations"]),
        ("approved", "Approved today", stats["approved_today"]),
        ("billed", "Billed today", stats["billed_today"]),
        ("late", "Late orders", stats["late_orders"]),
    ]
    cards = "".join(
        f"<div class='stat'><div class='n' id='stat-{key}'>{n}</div>"
        f"<div class='l'>{label}</div></div>"
        for key, label, n in rows
    )
    return (
        f"<div class='stats'>{cards}</div>"
        "<script>setInterval(async () => { "
        "try { const r = await fetch('/review/stats'); if (!r.ok) return; "
        "const s = await r.json(); "
        "for (const k of ['pending_escalations','approved_today',"
        "'billed_today','late_orders']) "
        "document.getElementById('stat-'+k).textContent = s[k]; "
        "} catch (e) {} }, 10000);</script>"
    )


def search_bar(q: str | None) -> str:
    """Search form; ``q`` is echoed back so the current query survives a reload."""
    value = escape(q) if q else ""
    return (
        f"<form class='searchbar' method='get' action='/review/orders'>"
        f"<input type='text' name='q' value='{value}' "
        f"placeholder='Search by order id, phone, customer, location or event'>"
        f"<button class='btn' type='submit'>Search</button></form>"
    )


def queue_page(orders: list[Order], stats: dict[str, int], q: str | None = None) -> str:
    """Escalation (or search-result) queue: a card per order with reason badges."""
    rows: list[str] = []
    for order in orders:
        status = STATUS_LABELS.get(order.status.value, order.status.value)
        rows.append(
            f"<div class='card'><div class='order-row'>"
            f"<a class='id' href='/review/orders/{_escaped(order.order_id)}'>"
            f"{_escaped(order.order_id)}</a>"
            f"<span class='badge n'>{escape(status)}</span></div>"
            f"<div class='meta'>{escape(order.phone)} \xb7 "
            f"{escape(order.customer or 'unknown customer')} \xb7 "
            f"{_items_summary(order)} \xb7 "
            f"est. {order.draft_value_inr:,.0f} INR</div>"
            f"{_reason_badges(order.escalation_reasons)}</div>"
        )
    if rows:
        list_html = "".join(rows)
    else:
        list_html = (
            "<div class='card empty-state'>"
            "<p>No orders found.</p></div>"
        )
    body = (
        f"<h1>Review</h1>"
        f"<p class='sub'>Live state from the Order Processing Core; "
        f"web decisions stay in sync with WhatsApp.</p>"
        f"{stat_bar(stats)}{search_bar(q)}"
        f"<h2>Orders</h2>{list_html}"
    )
    return page("Review", body)


def _voucher_card(order: Order, tally_push_url: str = "") -> str:
    """The billing-voucher actions on an order detail (issue #8).

    An approved order can have its Tally voucher prepared from here (the same
    seam the ADK tool uses); a prepared voucher is downloadable for manual Tally
    import, can be pushed directly to Tally if configured, and can be marked
    billed. Nothing is offered for a pending or rejected order.
    """
    order_id = _escaped(order.order_id)
    if order.voucher_id:
        voucher_id = _escaped(order.voucher_id)
        mark_billed = ""
        push_tally = ""
        if order.status in (OrderStatus.APPROVED, OrderStatus.DISPATCHED):
            mark_billed = (
                f"<form class='inline' method='post' "
                f"action='/review/orders/{order_id}/billed'>"
                f"<button class='btn' type='submit'>Mark billed</button></form>"
            )
        if tally_push_url:
            push_tally = (
                f"<form class='inline' method='post' "
                f"action='/review/orders/{order_id}/push-to-tally'>"
                f"<button class='approve' type='submit'>Push to Tally</button></form>"
            )
        body = (
            f"<p style='margin:0 0 var(--space-3)'>Voucher "
            f"<strong>{voucher_id}</strong> is ready.</p>"
            f"<a class='btn' href='/review/orders/{order_id}/voucher'>"
            f"Download voucher XML</a> {push_tally} {mark_billed}"
        )
    elif order.status is OrderStatus.APPROVED:
        body = (
            f"<form class='inline' method='post' "
            f"action='/review/orders/{order_id}/prepare-voucher'>"
            f"<button class='approve' type='submit'>Prepare voucher</button></form>"
        )
    else:
        body = (
            "<p class='sub' style='margin:0'>No voucher \u2014 the order is not "
            "approved.</p>"
        )
    return f"<div class='card'><h2>Tally voucher</h2>{body}</div>"


def order_page(
    order: Order,
    events: list[OrderEvent],
    message: str | None = None,
    notice: str | None = None,
    tally_push_url: str = "",
) -> str:
    """Order detail: fields, items, the Order Event timeline, and decision."""
    err = f"<div class='error'>{escape(message)}</div>" if message else ""
    ntc = f"<div class='notice'>{escape(notice)}</div>" if notice else ""
    status = STATUS_LABELS.get(order.status.value, order.status.value)
    items = "".join(
        f"<tr><td>{_escaped(item.product)}</td>"
        f"<td>{item.quantity:g} {escape(item.unit)}</td>"
        f"<td>{'—' if item.rate_inr is None else f'{item.rate_inr:,.2f}'}</td></tr>"
        for item in order.items
    )
    timeline = "".join(
        f"<tr><td style='white-space:nowrap;width:150px;color:var(--color-text-secondary)'>"
        f"{_escaped(event.created_at)}</td>"
        f"<td style='white-space:nowrap;width:200px;font-weight:600'>"
        f"{_escaped(event.event_type)}</td>"
        f"<td>{escape(str(event.payload))}</td></tr>"
        for event in events
    )
    actions = ""
    if order.status is OrderStatus.PENDING_REVIEW:
        actions = (
            f"<form class='inline' method='post' "
            f"action='/review/orders/{_escaped(order.order_id)}/approve'>"
            f"<button class='approve' type='submit'>Approve</button></form> "
            f"<form class='inline' method='post' "
            f"action='/review/orders/{_escaped(order.order_id)}/reject'>"
            f"<button class='reject' type='submit'>Reject</button></form> "
        )
    if order.status in (OrderStatus.PENDING_REVIEW, OrderStatus.REJECTED):
        actions += (
            f"<a class='btn' "
            f"href='/review/orders/{_escaped(order.order_id)}/edit'>"
            f"Edit order</a>"
        )
    back = "<p><a href='/review'>&larr; Back to review</a></p>"
    gst_override = (
        "\u2014" if order.gst_override_pct is None else f"{order.gst_override_pct:g}%"
    )
    body = (
        f"{back}{err}{ntc}"
        f"<div style='display:flex;align-items:baseline;gap:var(--space-3);"
        f"margin-bottom:var(--space-5)'>"
        f"<h1 style='margin:0'>Order {_escaped(order.order_id)}</h1>"
        f"<span class='badge n'>{escape(status)}</span></div>"
        f"<div class='card'><dl class='fields'>"
        f"<dt>Phone</dt><dd>{escape(order.phone)}</dd>"
        f"<dt>Customer</dt><dd>{escape(order.customer or '\u2014')}</dd>"
        f"<dt>Delivery location</dt><dd>{escape(order.delivery_location or '\u2014')}</dd>"
        f"<dt>Source channel</dt><dd>{escape(order.source_channel)}</dd>"
        f"<dt>Source language</dt><dd>{escape(order.source_language)}</dd>"
        f"<dt>Confidence</dt><dd>{order.confidence:g}</dd>"
        f"<dt>Estimated total</dt><dd>{order.draft_value_inr:,.0f} INR</dd>"
        f"<dt>GST override</dt><dd>{escape(gst_override)}</dd>"
        f"</dl>{_reason_badges(order.escalation_reasons)}</div>"
        f"<div class='card'><h2>Items</h2>"
        f"<table class='timeline'>"
        f"<tr><th style='width:50%'>Product</th><th style='width:30%'>Quantity</th>"
        f"<th style='width:20%'>Rate (INR)</th></tr>{items}</table></div>"
        f"{_voucher_card(order, tally_push_url)}"
        f"<div class='card'><h2>Order Event timeline</h2>"
        f"<table class='timeline'>{timeline}</table></div>"
        f"<div class='card'><h2>Decision</h2><div style='display:flex;"
        f"gap:var(--space-2);flex-wrap:wrap'>{actions}</div></div>"
    )
    return page(f"Order {order.order_id}", body)


# Number of blank item rows the edit form offers after the order's own lines,
# so an approver can add lines as well as correct them. Blank rows are dropped
# on submit.
EDIT_EXTRA_ROWS = 3


def _editable_lines(order: Order) -> list[OrderItem | None]:
    lines: list[OrderItem | None] = list(order.items)
    lines.extend([None] * EDIT_EXTRA_ROWS)
    return lines


def _item_edit_row(
    index: int, item: OrderItem | None, products: list
) -> str:
    product = item.product if item else ""
    quantity = f"{item.quantity:g}" if item else ""
    unit = item.unit if item else ""
    rate = "" if item is None or item.rate_inr is None else f"{item.rate_inr:g}"
    options = "".join(
        f"<option value='{_escaped(p.id)}'>{_escaped(p.name)} "
        f"({_escaped(p.grade)})</option>"
        for p in products
    )
    return (
        f"<tr><td><input type='text' name='items[{index}][product]' "
        f"value='{escape(product)}'></td>"
        f"<td><input type='text' name='items[{index}][quantity]' "
        f"value='{escape(quantity)}'></td>"
        f"<td><input type='text' name='items[{index}][unit]' "
        f"value='{escape(unit)}'></td>"
        f"<td><input type='text' name='items[{index}][rate_inr]' "
        f"value='{escape(rate)}'></td>"
        f"<td><select name='items[{index}][product_id]'>"
        f"<option value=''></option>{options}</select></td></tr>"
    )


def edit_page(
    order: Order,
    customers: list,
    products: list,
    message: str | None = None,
) -> str:
    """Order edit form (issue #6, follow-up): fields, items, GST override.

    Corrections are applied by POSTing to the same Order Processing Core the
    agent uses, so web edits stay in sync with WhatsApp decisions and land on
    the shared audit trail as an ``order_edited`` event. The resolve selects
    let an approver explicitly assign a catalog customer / product to an
    unknown one.
    """
    err = f"<div class='error'>{escape(message)}</div>" if message else ""
    back = (
        f"<p><a href='/review/orders/{_escaped(order.order_id)}'>"
        "&larr; Back to order</a></p>"
    )
    customer_options = "".join(
        f"<option value='{_escaped(c.id)}'>{_escaped(c.name)}</option>"
        for c in customers
    )
    rows = "".join(
        _item_edit_row(index, item, products)
        for index, item in enumerate(_editable_lines(order))
    )
    gst_value = (
        "" if order.gst_override_pct is None else f"{order.gst_override_pct:g}"
    )
    body = (
        f"{back}{err}"
        f"<h1>Edit order {_escaped(order.order_id)}</h1>"
        f"<form method='post' "
        f"action='/review/orders/{_escaped(order.order_id)}/edit'>"
        f"<div class='card'><dl class='fields'>"
        f"<dt>Customer</dt><dd>"
        f"<input type='text' name='customer' value='{_escaped(order.customer or '')}'>"
        f"<p class='sub' style='margin:var(--space-1) 0 0;font-size:var(--text-xs)'>"
        f"Resolve an unknown customer: "
        f"<select name='customer_id'><option value=''></option>"
        f"{customer_options}</select></p></dd>"
        f"<dt>Delivery location</dt><dd>"
        f"<input type='text' name='delivery_location' "
        f"value='{_escaped(order.delivery_location or '')}'></dd>"
        f"</dl></div>"
        f"<div class='card'><h2>Items</h2>"
        f"<table class='edit-items'><tr><th>Product</th><th>Quantity</th>"
        f"<th>Unit</th><th>Rate (INR)</th><th>Resolve product</th></tr>"
        f"{rows}</table>"
        f"<p class='sub' style='margin:var(--space-2) 0 0;font-size:var(--text-xs)'>"
        f"Leave a row's product blank to remove it; pick a product to map "
        f"an uncataloged line.</p></div>"
        f"<div class='card'><dl class='fields'>"
        f"<dt>GST override (%)</dt><dd>"
        f"<input type='number' name='gst_override_pct' min='0' max='100' "
        f"step='0.01' value='{escape(gst_value)}'>"
        f"<p class='sub' style='margin:var(--space-1) 0 0;font-size:var(--text-xs)'>"
        f"Overrides the GST rate the billing path derives "
        f"(issue #8); leave blank for the default.</p></dd>"
        f"</dl></div>"
        f"<button class='approve' type='submit'>Save changes</button></form>"
    )
    return page(f"Edit order {order.order_id}", body)
