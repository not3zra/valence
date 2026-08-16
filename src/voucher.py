"""Tally voucher generation (issue #8): GST computation + the voucher seam.

An approved order becomes a sales-invoice voucher on demand ("prepare voucher"
is an ADK tool, issue #8). Line amounts are locked from the two-tier money model
(agreed rate > pricing tier > catalog current price) at generation time — never
the draft estimates or the customer-stated rate; the GST split (CGST+SGST for
intra-state, IGST for inter-state) derives from the delivery-location state
(falling back to the customer state, then the customer's GSTIN) against the
configured seller state; and the voucher XML references only pre-seeded, mapped
masters — the customer's party ledger, each product+grade's stock item, and the
configured GST duty ledgers. Any unmapped master blocks generation with an
explicit message and no partial voucher is produced (ADR-0003).

The voucher is written to the ``VoucherStore`` seam (Cloud Storage in
production, the in-memory double in tests), stamped on the order as
``voucher_id``, and recorded as a ``voucher_ready`` Order Event. Import is the
manual download + file import path — there is no live network bridge to an
on-prem Tally in the demo.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from xml.sax.saxutils import escape

from google.cloud import storage as gcs_storage  # type: ignore[attr-defined]

from .core import resolve_product
from .loading import parse_business_tz
from .money import estimate_rate
from .orders import (
    EVENT_ORDER_APPROVED,
    EVENT_ORDER_AUTO_APPROVED,
    EVENT_VOUCHER_READY,
    Order,
    OrderEvent,
    OrderStatus,
    iso_to_dt,
)
from .seed_data import Customer, DeliveryLocation
from .store import OrderStore

# Sentinel for a master that has not been mapped to Tally yet (ADR-0003): the
# voucher must never reference it.
UNMAPPED = "UNMAPPED"

# The GSTIN's first two digits encode the taxpayer's state code; used only as
# the last-resort fallback for the intra/inter-state split when neither the
# delivery-location state nor the customer's state resolves (issue #8).
GSTIN_STATE_CODES: dict[str, str] = {
    "01": "Jammu and Kashmir",
    "02": "Himachal Pradesh",
    "03": "Punjab",
    "04": "Chandigarh",
    "05": "Uttarakhand",
    "06": "Haryana",
    "07": "Delhi",
    "08": "Rajasthan",
    "09": "Uttar Pradesh",
    "10": "Bihar",
    "11": "Sikkim",
    "12": "Arunachal Pradesh",
    "13": "Nagaland",
    "14": "Manipur",
    "15": "Mizoram",
    "16": "Tripura",
    "17": "Meghalaya",
    "18": "Assam",
    "19": "West Bengal",
    "20": "Jharkhand",
    "21": "Odisha",
    "22": "Chhattisgarh",
    "23": "Madhya Pradesh",
    "24": "Gujarat",
    "25": "Dadra and Nagar Haveli and Daman and Diu",
    "26": "Maharashtra",
    "27": "Maharashtra",
    "28": "Andhra Pradesh",
    "29": "Karnataka",
    "30": "Goa",
    "31": "Lakshadweep",
    "32": "Kerala",
    "33": "Tamil Nadu",
    "34": "Puducherry",
    "35": "Andaman and Nicobar Islands",
    "36": "Telangana",
    "37": "Andhra Pradesh",
    "38": "Ladakh",
}

# Every configured Tally master the voucher can reference, plus the seller
# identity used for the intra/inter-state split. A missing key is a hard block.
REQUIRED_VOUCHER_CONFIG_KEYS: tuple[str, ...] = (
    "seller_state",
    "gst_cgst_ledger",
    "gst_sgst_ledger",
    "gst_igst_ledger",
    "gst_sales_ledger",
)


class VoucherError(RuntimeError):
    """Raised when a voucher cannot be generated (unmapped master, bad state)."""


@dataclass(frozen=True)
class VoucherLine:
    """One voucher line: the mapped stock item with its locked amount."""

    stock_item: str
    quantity: float
    rate_inr: float
    amount_inr: float

    def to_dict(self) -> dict:
        return {
            "stock_item": self.stock_item,
            "quantity": self.quantity,
            "rate_inr": self.rate_inr,
            "amount_inr": self.amount_inr,
        }


@dataclass(frozen=True)
class GstComponent:
    """One GST rate's worth of tax, split as CGST+SGST or IGST."""

    rate_pct: float
    taxable_amount: float
    cgst_amount: float
    sgst_amount: float
    igst_amount: float

    def to_dict(self) -> dict:
        return {
            "rate_pct": self.rate_pct,
            "taxable_amount": self.taxable_amount,
            "cgst_amount": self.cgst_amount,
            "sgst_amount": self.sgst_amount,
            "igst_amount": self.igst_amount,
        }


@dataclass(frozen=True)
class Voucher:
    """A computed sales-invoice voucher for one approved order."""

    voucher_id: str
    order_id: str
    date: str  # YYYYMMDD, the order's approval day in the business timezone
    party_ledger: str
    lines: list[VoucherLine]
    gst_type: str  # "CGST" (cgst+sgst) or "IGST"
    gst: list[GstComponent]
    taxable_amount: float
    cgst_amount: float
    sgst_amount: float
    igst_amount: float
    total_amount: float
    ledger_cgst: str
    ledger_sgst: str
    ledger_igst: str
    ledger_sales: str
    narration: str  # the order_id — a mapped reference id, never chat text

    def to_dict(self) -> dict:
        return {
            "voucher_id": self.voucher_id,
            "order_id": self.order_id,
            "date": self.date,
            "party_ledger": self.party_ledger,
            "lines": [line.to_dict() for line in self.lines],
            "gst_type": self.gst_type,
            "gst": [component.to_dict() for component in self.gst],
            "taxable_amount": self.taxable_amount,
            "cgst_amount": self.cgst_amount,
            "sgst_amount": self.sgst_amount,
            "igst_amount": self.igst_amount,
            "total_amount": self.total_amount,
            "ledger_cgst": self.ledger_cgst,
            "ledger_sgst": self.ledger_sgst,
            "ledger_igst": self.ledger_igst,
            "ledger_sales": self.ledger_sales,
            "narration": self.narration,
        }


class VoucherStore(Protocol):
    """Outbound seam: persist and retrieve generated voucher XML."""

    async def write(self, voucher_id: str, xml: str) -> None: ...

    async def read(self, voucher_id: str) -> str | None: ...


class GcsVoucherStore:
    """VoucherStore backed by the Cloud Storage bucket the scaffold provisions.

    One blob per voucher, named ``{voucher_id}.xml``, stored with the XML media
    type so a manual download imports directly into Tally. The runtime service
    account holds ``roles/storage.objectUser`` on the project (provision.sh).
    """

    def __init__(self, bucket_name: str, client: gcs_storage.Client | None = None):
        self._bucket_name = bucket_name
        self._client = client or gcs_storage.Client()

    async def write(self, voucher_id: str, xml: str) -> None:
        bucket = self._client.bucket(self._bucket_name)
        bucket.blob(f"{voucher_id}.xml").upload_from_string(
            xml, content_type="application/xml"
        )

    async def read(self, voucher_id: str) -> str | None:
        bucket = self._client.bucket(self._bucket_name)
        blob = bucket.blob(f"{voucher_id}.xml")
        if not blob.exists():
            return None
        return blob.download_as_string().decode()


class InMemoryVoucherStore:
    """VoucherStore double: the shared in-memory/emulator/local demo path."""

    def __init__(self) -> None:
        self.blobs: dict[str, str] = {}

    async def write(self, voucher_id: str, xml: str) -> None:
        self.blobs[voucher_id] = xml

    async def read(self, voucher_id: str) -> str | None:
        return self.blobs.get(voucher_id)


def default_voucher_storage(bucket: str = "") -> VoucherStore:
    """The production Cloud Storage store, or the in-memory double.

    The bucket is provisioned by the scaffold (``valence-<project>-vouchers``);
    when none is configured (a fresh clone, local dev, tests) the in-memory
    double keeps the service runnable and the voucher still downloadable for
    the manual Tally import path.
    """
    return GcsVoucherStore(bucket) if bucket else InMemoryVoucherStore()


def _round2(value: float) -> float:
    return round(value, 2)


def _customer_by_id(
    customer_id: str | None, customers: list[Customer]
) -> Customer | None:
    if customer_id is None:
        return None
    for customer in customers:
        if customer.id == customer_id:
            return customer
    return None


def _state_from_gstin(gstin: str) -> str | None:
    """The state name a GSTIN's first two digits encode, if they resolve."""
    if not gstin or len(gstin) < 2 or not gstin[:2].isdigit():
        return None
    return GSTIN_STATE_CODES.get(gstin[:2])


def supply_state_for(
    order: Order,
    customer: Customer | None,
    delivery_locations: list[DeliveryLocation],
) -> str | None:
    """The GST place-of-supply state for an order.

    The delivery-location state is authoritative; when the order's delivery
    location id no longer resolves (issue #8 fallback), the customer's state is
    used, and failing that the state encoded in the customer's GSTIN. ``None``
    when none resolves — a blocked generation.
    """
    location_by_id = {location.id: location for location in delivery_locations}
    location = location_by_id.get(order.delivery_location_id or "")
    if location is not None and location.state:
        return location.state
    if customer is not None and customer.state:
        return customer.state
    if customer is not None:
        return _state_from_gstin(customer.gstin)
    return None


def gst_type(seller_state: str, supply_state: str) -> str:
    """CGST+SGST for an intra-state supply, IGST for inter-state."""
    return "CGST" if seller_state == supply_state else "IGST"


def split_tax(
    split: str, rate_pct: float, taxable: float
) -> tuple[float, float, float]:
    """Split one rate bucket's GST into (cgst, sgst, igst).

    The single place the CGST+SGST / IGST branching lives: the component build
    and the ledger rendering both read from it, so the two halves can never
    drift apart.
    """
    if split == "CGST":
        half = _round2(taxable * rate_pct / 200)
        return half, half, 0.0
    return 0.0, 0.0, _round2(taxable * rate_pct / 100)


def _voucher_id(order_id: str) -> str:
    return f"voucher_{order_id}"


async def prepare_voucher(
    store: OrderStore,
    storage: VoucherStore,
    order_id: str,
) -> Voucher:
    """Generate and persist the Tally voucher for one approved order.

    The one seam the ADK tool and the review web view share (issue #8): read
    the live order and masters, lock the authoritative line amounts from the
    two-tier money model, derive the GST split from the delivery-location state
    (honouring the web-view ``gst_override_pct``), build the voucher XML from
    only the mapped masters, write it to ``storage``, stamp ``voucher_id`` on
    the order, and record a ``voucher_ready`` Order Event.

    Generation blocks with a clear ``VoucherError`` — and writes nothing — when
    the order is not approved, a voucher already exists, or any master is
    unmapped: the customer's party ledger, a product's stock item, the seller
    state, or one of the configured GST ledgers.
    """
    order = await store.get_order(order_id)
    if order is None:
        raise VoucherError(f"order {order_id} not found")
    if order.status is not OrderStatus.APPROVED:
        raise VoucherError(f"order {order_id} is {order.status.value}, not approved")
    if order.voucher_id:
        raise VoucherError(f"order {order_id} already has voucher {order.voucher_id}")
    # The stored order always carries a system-generated id (``ord_<hex>``);
    # block rather than ever let a caller-supplied ``order_id`` reach the blob
    # name or the voucher XML unvalidated.
    real_id = order.order_id
    if not real_id:
        raise VoucherError(f"order {order_id} is missing its order id")

    config = await store.get_config()
    missing = [key for key in REQUIRED_VOUCHER_CONFIG_KEYS if not config.get(key)]
    if missing:
        raise VoucherError("voucher masters not configured: " + ", ".join(missing))
    seller_state = str(config["seller_state"])
    ledger_cgst = str(config["gst_cgst_ledger"])
    ledger_sgst = str(config["gst_sgst_ledger"])
    ledger_igst = str(config["gst_igst_ledger"])
    ledger_sales = str(config["gst_sales_ledger"])

    customers = await store.get_customers()
    customer = _customer_by_id(order.customer_id, customers)
    if customer is None:
        raise VoucherError(
            f"order {order_id} has no mapped customer "
            f"(customer {order.customer!r} is not in the catalog)"
        )
    if not customer.ledger or customer.ledger == UNMAPPED:
        raise VoucherError(f"customer {customer.name} has no mapped Tally party ledger")

    products = await store.get_products()
    lines: list[VoucherLine] = []
    by_rate: dict[float, float] = {}
    for item in order.items:
        product = resolve_product(item.product, products)
        if product is None:
            raise VoucherError(
                f"product {item.product!r} on order {order_id} is not mapped"
            )
        if not product.stock_item or product.stock_item == UNMAPPED:
            raise VoucherError(f"product {product.name} has no mapped Tally stock item")
        rate = estimate_rate(
            customer.agreed_rates.get(product.id),
            product.tier_price,
            product.current_price,
        )
        line = VoucherLine(
            stock_item=product.stock_item,
            quantity=item.quantity,
            rate_inr=rate,
            amount_inr=_round2(rate * item.quantity),
        )
        lines.append(line)
        # GST is grouped per rate so a web-view override (or a future mixed-rate
        # order) yields one balanced ledger pair per rate, never a single wrong
        # one. The override applies even at 0%.
        rate_pct = (
            order.gst_override_pct
            if order.gst_override_pct is not None
            else product.gst_pct
        )
        by_rate[rate_pct] = _round2(by_rate.get(rate_pct, 0.0) + line.amount_inr)

    locations = await store.get_delivery_locations()
    supply_state = supply_state_for(order, customer, locations)
    if not supply_state:
        raise VoucherError(
            f"cannot resolve a supply state for order {order_id}'s GST split"
        )
    split = gst_type(seller_state, supply_state)

    gst: list[GstComponent] = []
    for rate_pct, taxable in sorted(by_rate.items()):
        cgst, sgst, igst = split_tax(split, rate_pct, taxable)
        gst.append(
            GstComponent(
                rate_pct=rate_pct,
                taxable_amount=taxable,
                cgst_amount=cgst,
                sgst_amount=sgst,
                igst_amount=igst,
            )
        )

    taxable_amount = _round2(sum(line.amount_inr for line in lines))
    cgst_amount = _round2(sum(component.cgst_amount for component in gst))
    sgst_amount = _round2(sum(component.sgst_amount for component in gst))
    igst_amount = _round2(sum(component.igst_amount for component in gst))
    total_amount = _round2(taxable_amount + cgst_amount + sgst_amount + igst_amount)

    business_tz = parse_business_tz(config)
    date = await _approval_day(store, real_id, order, business_tz)
    voucher_id = _voucher_id(real_id)
    voucher = Voucher(
        voucher_id=voucher_id,
        order_id=real_id,
        date=date,
        party_ledger=customer.ledger,
        lines=lines,
        gst_type=split,
        gst=gst,
        taxable_amount=taxable_amount,
        cgst_amount=cgst_amount,
        sgst_amount=sgst_amount,
        igst_amount=igst_amount,
        total_amount=total_amount,
        ledger_cgst=ledger_cgst,
        ledger_sgst=ledger_sgst,
        ledger_igst=ledger_igst,
        ledger_sales=ledger_sales,
        narration=real_id,
    )

    await storage.write(voucher_id, build_voucher_xml(voucher))
    order.voucher_id = voucher_id
    await store.update_order(order)
    await store.append_order_event(
        OrderEvent(
            order_id=real_id,
            event_type=EVENT_VOUCHER_READY,
            payload={
                "voucher_id": voucher_id,
                "order_id": real_id,
                "total_amount": total_amount,
            },
        )
    )
    return voucher


async def _approval_day(
    store: OrderStore, order_id: str, order: Order, business_tz
) -> str:
    """The day the order was approved, in the business timezone (invoice date).

    ``order.updated_at`` is bumped by later status changes (dispatch) and web
    edits, so the voucher date is read from the approval event's timestamp;
    ``updated_at`` is the last-resort fallback when the event is missing.
    """
    events = await store.list_order_events(order_id)
    approval = next(
        (
            event
            for event in events
            if event.event_type in (EVENT_ORDER_APPROVED, EVENT_ORDER_AUTO_APPROVED)
        ),
        None,
    )
    stamp = approval.created_at if approval is not None else order.updated_at
    return iso_to_dt(stamp).astimezone(business_tz).strftime("%Y%m%d")


def build_voucher_xml(voucher: Voucher) -> str:
    """Render the voucher as a Tally import XML document.

    The voucher references only mapped masters: the customer's party ledger,
    the product+grade stock items, and the configured GST duty ledgers (the
    sales allocation ledger and the order id as narration). Master names are
    XML-escaped; no chat text ever reaches the document.
    """

    def _ledger(name: str, amount: float) -> str:
        return (
            f"<LEDGERENTRIES.LIST><LEDGERNAME>{escape(name)}</LEDGERNAME>"
            f"<AMOUNT>{amount:.2f}</AMOUNT></LEDGERENTRIES.LIST>"
        )

    ledger_entries = [_ledger(voucher.party_ledger, -voucher.total_amount)]
    for component in voucher.gst:
        # Keyed off the amounts the component already carries, so the CGST/SGST
        # vs IGST split can never disagree with ``split_tax``.
        if component.igst_amount:
            ledger_entries.append(_ledger(voucher.ledger_igst, component.igst_amount))
        if component.cgst_amount:
            ledger_entries.append(_ledger(voucher.ledger_cgst, component.cgst_amount))
            ledger_entries.append(_ledger(voucher.ledger_sgst, component.sgst_amount))

    inventory_entries = []
    for line in voucher.lines:
        inventory_entries.append(
            "<INVENTORYENTRIES.LIST>"
            f"<STOCKITEMNAME>{escape(line.stock_item)}</STOCKITEMNAME>"
            f"<RATE>{line.rate_inr:.2f}</RATE>"
            f"<QUANTITY>{line.quantity:g}</QUANTITY>"
            f"<AMOUNT>{line.amount_inr:.2f}</AMOUNT>"
            "<ACCOUNTINGALLOCATIONS.LIST>"
            f"<LEDGERNAME>{escape(voucher.ledger_sales)}</LEDGERNAME>"
            f"<AMOUNT>{line.amount_inr:.2f}</AMOUNT>"
            "</ACCOUNTINGALLOCATIONS.LIST>"
            "</INVENTORYENTRIES.LIST>"
        )

    return (
        "<ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>"
        "<BODY><IMPORTDATA><REQUESTDESC><REPORTNAME>Vouchers</REPORTNAME>"
        "</REQUESTDESC><REQUESTDATA><TALLYMESSAGE>"
        f'<VOUCHER VCHTYPE="Sales" ACTION="Create">'
        f"<VOUCHERNUMBER>{escape(voucher.voucher_id)}</VOUCHERNUMBER>"
        f"<DATE>{voucher.date}</DATE>"
        f"<PARTYLEDGERNAME>{escape(voucher.party_ledger)}</PARTYLEDGERNAME>"
        f"<VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>"
        f"<NARRATION>{escape(voucher.narration)}</NARRATION>"
        f"<AMOUNT>{voucher.total_amount:.2f}</AMOUNT>"
        f"{''.join(ledger_entries)}"
        f"{''.join(inventory_entries)}"
        "</VOUCHER></TALLYMESSAGE></REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>"
    )
