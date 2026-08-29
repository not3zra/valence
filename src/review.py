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

_BASE_CSS = """
:root { color-scheme: light; }
body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
       margin: 0; background: #f6f7f9; color: #1a202c; }
.wrap { max-width: 980px; margin: 0 auto; padding: 24px 20px 60px; }
header { background: #111827; color: #f9fafb; padding: 14px 20px; }
header a { color: #e5e7eb; margin-right: 18px; text-decoration: none; }
header a:hover { color: #fff; }
header form { display: inline; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 18px; margin: 26px 0 10px; }
.sub { color: #6b7280; margin: 0 0 18px; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px,1fr));
         gap: 12px; margin: 18px 0 8px; }
.stat { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px;
        padding: 14px 16px; }
.stat .n { font-size: 26px; font-weight: 600; }
.stat .l { color: #6b7280; font-size: 13px; }
.card { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px;
        padding: 16px; margin-bottom: 12px; }
.order-row { display: flex; justify-content: space-between; gap: 12px;
             align-items: baseline; }
.order-row .id { font-weight: 600; }
.meta { color: #6b7280; font-size: 13px; margin-top: 6px; }
.badges { margin-top: 8px; }
.badge { display: inline-block; border-radius: 999px; padding: 2px 10px;
         margin-right: 6px; font-size: 12px; font-weight: 600;
         background: #fef3c7; color: #92400e; }
.badge.b { background: #fee2e2; color: #b91c1c; }
.badge.g { background: #dcfce7; color: #166534; }
.badge.n { background: #e0e7ff; color: #3730a3; }
.badge.d { background: #f3e8ff; color: #6b21a8; }
table.timeline { width: 100%; border-collapse: collapse; font-size: 14px; }
table.timeline td { border-top: 1px solid #eef2f6; padding: 8px 10px;
                    vertical-align: top; }
table.timeline .when { color: #6b7280; white-space: nowrap; width: 150px; }
table.timeline .type { font-weight: 600; white-space: nowrap; width: 200px; }
form.inline { display: inline; }
button, .btn { border-radius: 6px; border: 1px solid #cbd5e1; background: #fff;
       padding: 6px 14px; font-size: 14px; cursor: pointer; }
button.approve { background: #16a34a; border-color: #16a34a; color: #fff; }
button.reject { background: #dc2626; border-color: #dc2626; color: #fff; }
input[type=text], input[type=password] { border: 1px solid #cbd5e1;
       border-radius: 6px; padding: 8px 10px; font-size: 14px; }
.searchbar { display: flex; gap: 8px; margin: 16px 0; }
.error { background: #fee2e2; color: #b91c1c; border: 1px solid #fecaca;
         border-radius: 6px; padding: 10px 14px; margin: 12px 0; }
.notice { background: #dcfce7; color: #166534; border: 1px solid #bbf7d0;
         border-radius: 6px; padding: 10px 14px; margin: 12px 0; }
dl { display: grid; grid-template-columns: 140px 1fr; gap: 6px 12px;
     font-size: 14px; }
dt { color: #6b7280; font-weight: 600; }
dd { margin: 0; }
.login-card { max-width: 360px; margin: 60px auto; }
input[type=number], select { border: 1px solid #cbd5e1; border-radius: 6px;
        padding: 8px 10px; font-size: 14px; }
.edit-items { width: 100%; border-collapse: collapse; }
.edit-items th { text-align: left; color: #6b7280; font-size: 13px;
        padding: 4px 8px; }
.edit-items td { padding: 6px 8px 6px 0; }
.edit-items input { width: 100%; box-sizing: border-box; }
"""


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
    return escape(", ".join(parts)) if parts else "—"


def page(title: str, body: str) -> str:
    """Wrap ``body`` in the shared review shell (header, nav, logout, CSS)."""
    return (
        f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{escape(title)} — Valence Review</title>"
        f"<style>{_BASE_CSS}</style></head><body>"
        f"<header><a href='/review'>Review</a>"
        f"<a href='/review/orders'>All orders</a>"
        f"<form method='post' action='/review/logout'>"
        f"<button class='btn' type='submit'>Log out</button></form></header>"
        f"<div class='wrap'>{body}</div></body></html>"
    )


def login_page(error: str | None = None) -> str:
    """Passcode login form; ``error`` renders a banner above the field."""
    err = f"<div class='error'>{escape(error)}</div>" if error else ""
    body = (
        f"<div class='login-card card'><h1>Valence Review</h1>"
        f"<p class='sub'>Enter the demo passcode to open the escalation queue.</p>"
        f"{err}"
        f"<form method='post' action='/review/login'>"
        f"<input type='password' name='passcode' placeholder='Passcode' "
        f"autocomplete='current-password' required> "
        f"<button class='btn' type='submit'>Enter</button></form></div>"
    )
    return page("Log in", body)


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
            f"<div class='meta'>phone {escape(order.phone)} · "
            f"{escape(order.customer or 'unknown customer')} · "
            f"{_items_summary(order)} · "
            f"est. {order.draft_value_inr:,.0f} INR</div>"
            f"{_reason_badges(order.escalation_reasons)}</div>"
        )
    list_html = "".join(rows) if rows else (
        "<div class='card'><p class='sub' style='margin:0'>No orders found.</p></div>"
    )
    body = (
        f"<h1>Review</h1><p class='sub'>Live state from the Order Processing "
        f"Core; web decisions stay in sync with WhatsApp.</p>"
        f"{stat_bar(stats)}{search_bar(q)}<h2>Orders</h2>{list_html}"
    )
    return page("Review", body)


def _voucher_card(order: Order) -> str:
    """The billing-voucher actions on an order detail (issue #8).

    An approved order can have its Tally voucher prepared from here (the same
    seam the ADK tool uses); a prepared voucher is downloadable for manual Tally
    import and can be marked billed. Nothing is offered for a pending or
    rejected order.
    """
    order_id = _escaped(order.order_id)
    if order.voucher_id:
        voucher_id = _escaped(order.voucher_id)
        mark_billed = ""
        if order.status in (OrderStatus.APPROVED, OrderStatus.DISPATCHED):
            mark_billed = (
                f"<form class='inline' method='post' "
                f"action='/review/orders/{order_id}/billed'>"
                f"<button class='btn' type='submit'>Mark billed</button></form>"
            )
        body = (
            f"<p>Voucher <strong>{voucher_id}</strong> is ready.</p>"
            f"<a class='btn' href='/review/orders/{order_id}/voucher'>"
            f"Download voucher XML</a> {mark_billed}"
        )
    elif order.status is OrderStatus.APPROVED:
        body = (
            f"<form class='inline' method='post' "
            f"action='/review/orders/{order_id}/prepare-voucher'>"
            f"<button class='approve' type='submit'>Prepare voucher</button></form>"
        )
    else:
        body = (
            "<p class='sub' style='margin:0'>No voucher — the order is not "
            "approved.</p>"
        )
    return f"<div class='card'><h2>Tally voucher</h2>{body}</div>"


def order_page(
    order: Order,
    events: list[OrderEvent],
    message: str | None = None,
    notice: str | None = None,
) -> str:
    """Order detail: fields, items, the Order Event timeline, and decision."""
    err = f"<div class='error'>{escape(message)}</div>" if message else ""
    ntc = f"<div class='notice'>{escape(notice)}</div>" if notice else ""
    status = STATUS_LABELS.get(order.status.value, order.status.value)
    items = "".join(
        f"<tr><td>{_escaped(item.product)}</td>"
        f"<td>{item.quantity:g} {escape(item.unit)}</td>"
        f"<td>{'' if item.rate_inr is None else f'{item.rate_inr:,.2f} INR'}</td></tr>"
        for item in order.items
    )
    timeline = "".join(
        f"<tr><td class='when'>{_escaped(event.created_at)}</td>"
        f"<td class='type'>{_escaped(event.event_type)}</td>"
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
        "—" if order.gst_override_pct is None else f"{order.gst_override_pct:g}%"
    )
    body = (
        f"{back}{err}{ntc}<h1>Order {_escaped(order.order_id)}</h1>"
        f"<div class='card'><dl>"
        f"<dt>Status</dt><dd>{escape(status)}</dd>"
        f"<dt>Phone</dt><dd>{escape(order.phone)}</dd>"
        f"<dt>Customer</dt><dd>{escape(order.customer or '—')}</dd>"
        f"<dt>Delivery location</dt><dd>{escape(order.delivery_location or '—')}</dd>"
        f"<dt>Source channel</dt><dd>{escape(order.source_channel)}</dd>"
        f"<dt>Source language</dt><dd>{escape(order.source_language)}</dd>"
        f"<dt>Confidence</dt><dd>{order.confidence:g}</dd>"
        f"<dt>Estimated total</dt><dd>{order.draft_value_inr:,.0f} INR</dd>"
        f"<dt>GST override</dt><dd>{escape(gst_override)}</dd>"
        + (
            f"<dt>Transcription</dt><dd>{escape(order.transcription)}</dd>"
            if order.transcription
            else ""
        )
        + f"</dl>{_reason_badges(order.escalation_reasons)}</div>"
        f"<div class='card'><h2>Items</h2><table class='timeline'>"
        f"<tr><th>Product</th><th>Quantity</th><th>Rate</th></tr>{items}</table></div>"
        f"{_voucher_card(order)}"
        f"<div class='card'><h2>Order Event timeline</h2>"
        f"<table class='timeline'>{timeline}</table></div>"
        f"<div class='card'><h2>Decision</h2>{actions}</div>"
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
        f"{back}{err}<h1>Edit order {_escaped(order.order_id)}</h1>"
        f"<form method='post' "
        f"action='/review/orders/{_escaped(order.order_id)}/edit'>"
        f"<div class='card'><dl>"
        f"<dt>Customer</dt><dd>"
        f"<input type='text' name='customer' value='{_escaped(order.customer or '')}'>"
        f"<p class='sub' style='margin:6px 0 0'>Resolve an unknown customer: "
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
        f"<p class='sub' style='margin:8px 0 0'>Leave a row's product blank "
        f"to remove it; pick a product to map an uncataloged line.</p></div>"
        f"<div class='card'><dl>"
        f"<dt>GST override (%)</dt><dd>"
        f"<input type='number' name='gst_override_pct' min='0' max='100' "
        f"step='0.01' value='{escape(gst_value)}'></dd>"
        f"<dd class='sub'>Overrides the GST rate the billing path derives "
        f"(issue #8); leave blank for the default.</dd>"
        f"</dl></div>"
        f"<button class='approve' type='submit'>Save changes</button></form>"
    )
    return page(f"Edit order {order.order_id}", body)
