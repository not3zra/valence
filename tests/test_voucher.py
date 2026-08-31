"""Tally voucher generation (issue #8): GST computation + the voucher seam.

An approved order becomes a sales-invoice voucher on demand: line amounts are
locked from the two-tier money model (agreed rate > tier > catalog price) at
generation time — never the draft estimates or the customer-stated rate — the
GST split (CGST+SGST for intra-state, IGST for inter-state) derives from the
delivery-location state (falling back to the customer state, then the GSTIN)
against the configured seller state, and the voucher XML references only
pre-seeded, mapped masters. Any unmapped master blocks generation with an
explicit message and no partial voucher is produced.
"""

from __future__ import annotations

from dataclasses import replace
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import pytest

from src.core import OrderProcessingCore
from src.orders import (
    EVENT_ORDER_AUTO_APPROVED,
    EVENT_VOUCHER_READY,
    Order,
    OrderItem,
    iso_to_dt,
)
from src.seed_data import CONFIG, CUSTOMERS, DELIVERY_LOCATIONS, PRODUCTS
from src.store import InMemoryOrderStore
from src.voucher import (
    GcsVoucherStore,
    InMemoryVoucherStore,
    VoucherError,
    build_voucher_xml,
    gst_type,
    prepare_voucher,
    supply_state_for,
)


def _store():
    return InMemoryOrderStore()


def _storage():
    return InMemoryVoucherStore()


def _customer(phone: str = "+919812345001") -> dict:
    for customer in CUSTOMERS:
        if customer.phone == phone:
            return {
                "customer_id": customer.id,
                "customer": customer.name,
                "state": customer.state,
            }
    raise AssertionError(f"no seeded customer for {phone}")


async def _approved(
    store,
    *,
    phone: str = "+919812345001",
    items: list[OrderItem] | None = None,
    delivery_location: str = "Peenya Industrial Area",
    gst_override_pct: float | None = None,
    draft_value_inr: float = 0.0,
    rate_inr: float | None = None,
) -> Order:
    if items is None:
        items = [
            OrderItem(
                product="sulfuric acid",
                quantity=2000,
                unit="kg",
                rate_inr=rate_inr,
            )
        ]
    core = OrderProcessingCore(store)
    decision = await core.process(
        Order(
            phone=phone,
            customer=_customer(phone)["customer"],
            items=items,
            delivery_location=delivery_location,
            confidence=0.9,
        )
    )
    assert decision.approved, decision.escalation_reasons
    order = await store.get_order(decision.order_id)
    assert order is not None
    order.gst_override_pct = gst_override_pct
    order.draft_value_inr = draft_value_inr
    await store.update_order(order)
    return order


async def _prepare(
    store,
    storage,
    order_id: str,
):
    return await prepare_voucher(store, storage, order_id)


def test_gst_type_intra_state_is_cgst_plus_sgst():
    assert gst_type("Karnataka", "Karnataka") == "CGST"


def test_gst_type_inter_state_is_igst():
    assert gst_type("Karnataka", "Maharashtra") == "IGST"


def test_supply_state_prefers_delivery_location_over_customer_state():
    maruthi = next(c for c in CUSTOMERS if c.id == "c_maruthi")  # Maharashtra
    order = Order(
        phone=maruthi.phone,
        items=[],
        customer=maruthi.name,
        customer_id=maruthi.id,
        delivery_location="Peenya Industrial Area",
        delivery_location_id="dl_peenya",  # Karnataka
    )
    assert supply_state_for(order, maruthi, DELIVERY_LOCATIONS) == "Karnataka"


def test_supply_state_falls_back_to_customer_state():
    maruthi = next(c for c in CUSTOMERS if c.id == "c_maruthi")  # Maharashtra
    order = Order(
        phone=maruthi.phone,
        items=[],
        customer=maruthi.name,
        customer_id=maruthi.id,
        delivery_location="Somewhere",
        delivery_location_id="dl_removed",  # no longer seeded
    )
    assert supply_state_for(order, maruthi, DELIVERY_LOCATIONS) == "Maharashtra"


def test_supply_state_falls_back_to_customer_gstin_when_state_unknown():
    # Maruthi's GSTIN starts with 27 (Maharashtra); with no delivery location
    # and no state on the master, the GSTIN still resolves the supply state.
    maruthi = next(c for c in CUSTOMERS if c.id == "c_maruthi")
    no_state = replace(maruthi, state="")
    order = Order(
        phone=maruthi.phone,
        items=[],
        customer=maruthi.name,
        customer_id=maruthi.id,
        delivery_location_id="dl_removed",
    )
    assert supply_state_for(order, no_state, DELIVERY_LOCATIONS) == "Maharashtra"


def test_supply_state_returns_none_with_no_resolvable_state():
    maruthi = next(c for c in CUSTOMERS if c.id == "c_maruthi")
    no_state = replace(maruthi, state="", gstin="")
    order = Order(phone=maruthi.phone, items=[], customer_id=maruthi.id)
    assert supply_state_for(order, no_state, DELIVERY_LOCATIONS) is None


async def test_voucher_for_approved_order_is_valid_balanced_xml():
    store = _store()
    storage = _storage()
    order = await _approved(store)

    voucher = await _prepare(store, storage, order.order_id)

    assert voucher.order_id == order.order_id
    assert voucher.party_ledger == "CHEMFAB INDUSTRIES"
    assert [line.stock_item for line in voucher.lines] == ["Sulfuric Acid 98%"]
    assert voucher.taxable_amount == pytest.approx(17.5 * 2000)
    assert voucher.gst_type == "CGST"
    assert voucher.cgst_amount == pytest.approx(round(17.5 * 2000 * 0.09, 2))
    assert voucher.sgst_amount == pytest.approx(round(17.5 * 2000 * 0.09, 2))
    assert voucher.igst_amount == 0.0
    assert voucher.total_amount == pytest.approx(
        voucher.taxable_amount + voucher.cgst_amount + voucher.sgst_amount
    )

    xml = build_voucher_xml(voucher)
    root = ET.fromstring(xml)  # well-formed
    assert root.tag == "ENVELOPE"
    serialized = xml
    assert "CHEMFAB INDUSTRIES" in serialized
    assert "Sulfuric Acid 98%" in serialized
    assert "Output CGST 9%" in serialized
    assert "Output SGST 9%" in serialized
    assert "Output IGST 18%" not in serialized
    assert order.order_id in serialized


async def test_voucher_locks_authoritative_rate_not_the_stated_rate():
    # The customer stated a different rate (18.0); the voucher must lock the
    # agreed rate (17.5 for ChemFab sulfuric), never the stated one (issue #8).
    store = _store()
    storage = _storage()
    order = await _approved(
        store,
        items=[
            OrderItem(product="sulfuric acid", quantity=2000, unit="kg", rate_inr=18.0)
        ],
    )

    voucher = await _prepare(store, storage, order.order_id)

    assert [line.rate_inr for line in voucher.lines] == [17.5]
    assert voucher.taxable_amount == pytest.approx(17.5 * 2000)


async def test_voucher_ignores_the_stored_draft_estimate():
    # The draft estimate is a snapshot from intake; the voucher recomputes from
    # the live masters and must not inherit a stale stored total.
    store = _store()
    storage = _storage()
    order = await _approved(store, draft_value_inr=999999.0)

    voucher = await _prepare(store, storage, order.order_id)

    assert voucher.taxable_amount != 999999.0
    assert voucher.taxable_amount == pytest.approx(17.5 * 2000)


async def test_voucher_uses_tier_when_no_agreed_rate():
    # Swastik has no agreed rate for sulfuric acid: tier price (17.75) applies.
    store = _store()
    storage = _storage()
    order = await _approved(
        store,
        phone="+919812345003",
        items=[OrderItem(product="sulfuric acid", quantity=1000, unit="kg")],
        delivery_location="Bommasandra Industrial Area",
    )

    voucher = await _prepare(store, storage, order.order_id)

    assert [line.rate_inr for line in voucher.lines] == [17.75]


async def test_voucher_uses_catalog_price_when_no_tier():
    # Maruthi has no agreed rate for xylene and xylene has no tier: the catalog
    # current price (67.0) applies.
    store = _store()
    storage = _storage()
    order = await _approved(
        store,
        phone="+919812345002",  # Maruthi has no agreed rate for xylene
        items=[OrderItem(product="xylene", quantity=100, unit="L")],
        delivery_location="Bommasandra Industrial Area",
    )

    voucher = await _prepare(store, storage, order.order_id)

    assert [line.rate_inr for line in voucher.lines] == [67.0]


async def test_gst_split_follows_delivery_location_state():
    # Inter-state delivery (Maharashtra buyer at a Karnataka location) is still
    # intra-state: the split follows the delivery location, not the customer.
    store = _store()
    storage = _storage()
    order = await _approved(
        store,
        phone="+919812345002",
        items=[OrderItem(product="sulfuric acid", quantity=1000, unit="kg")],
        delivery_location="Peenya Industrial Area",
    )

    voucher = await _prepare(store, storage, order.order_id)

    assert voucher.gst_type == "CGST"
    assert voucher.igst_amount == 0.0


async def test_gst_inter_state_delivery_uses_igst():
    # All seeded locations are Karnataka, so prove IGST with a Maharashtra
    # location; the split follows the delivery location's state.
    store = _store()
    store.delivery_locations = [
        *DELIVERY_LOCATIONS,
        type(DELIVERY_LOCATIONS[0])(
            id="dl_out",
            name="Outside State",
            route_id="r_mysuru",
            address="Somewhere",
            state="Maharashtra",
        ),
    ]
    storage = _storage()
    order = await _approved(
        store,
        phone="+919812345001",
        items=[OrderItem(product="sulfuric acid", quantity=1000, unit="kg")],
        delivery_location="Outside State",
    )

    voucher = await _prepare(store, storage, order.order_id)

    assert voucher.gst_type == "IGST"
    assert voucher.cgst_amount == 0.0
    assert voucher.sgst_amount == 0.0
    assert voucher.igst_amount == pytest.approx(round(17.5 * 1000 * 0.18, 2))


async def test_gst_override_applies_to_the_voucher():
    store = _store()
    storage = _storage()
    order = await _approved(
        store,
        items=[OrderItem(product="sulfuric acid", quantity=2000, unit="kg")],
        gst_override_pct=5.0,
    )

    voucher = await _prepare(store, storage, order.order_id)

    assert len(voucher.gst) == 1
    assert voucher.gst[0].rate_pct == 5.0
    assert voucher.cgst_amount == pytest.approx(round(17.5 * 2000 * 0.025, 2))
    assert voucher.sgst_amount == pytest.approx(round(17.5 * 2000 * 0.025, 2))


async def test_gst_override_of_zero_is_honoured():
    # The review form allows a 0% override; it must zero out the tax, not fall
    # back to the product's standard rate.
    store = _store()
    storage = _storage()
    order = await _approved(
        store,
        items=[OrderItem(product="sulfuric acid", quantity=2000, unit="kg")],
        gst_override_pct=0.0,
    )

    voucher = await _prepare(store, storage, order.order_id)

    assert [component.rate_pct for component in voucher.gst] == [0.0]
    assert voucher.cgst_amount == 0.0
    assert voucher.sgst_amount == 0.0
    assert voucher.igst_amount == 0.0
    assert voucher.total_amount == pytest.approx(17.5 * 2000)


async def test_voucher_date_is_the_approval_day_not_a_later_status_stamp():
    # Dispatch (and web edits) bump ``updated_at``; the invoice date must stay
    # the day the order was approved.
    store = _store()
    storage = _storage()
    order = await _approved(store)
    approval = next(
        e
        for e in store.events
        if e.event_type == EVENT_ORDER_AUTO_APPROVED and e.order_id == order.order_id
    )
    order.updated_at = "2030-01-15T12:00:00+00:00"  # a different day
    await store.update_order(order)

    voucher = await _prepare(store, storage, order.order_id)

    expected = (
        iso_to_dt(approval.created_at)
        .astimezone(ZoneInfo("Asia/Kolkata"))
        .strftime("%Y%m%d")
    )
    assert voucher.date == expected
    assert voucher.date != "20300115"


async def test_unmapped_customer_blocks_generation_with_no_partial_voucher():
    store = _store()
    storage = _storage()
    order = await _approved(store)
    order.customer_id = "c_unknown"
    await store.update_order(order)

    with pytest.raises(VoucherError, match="mapped customer"):
        await _prepare(store, storage, order.order_id)
    assert storage.blobs == {}
    assert (await store.get_order(order.order_id)).voucher_id is None


async def test_customer_without_a_ledger_blocks_generation():
    store = _store()
    storage = _storage()
    order = await _approved(store)
    order.customer_id = "c_noledger"
    await store.update_order(order)
    store.customers = [
        *CUSTOMERS,
        type(CUSTOMERS[0])(
            id="c_noledger",
            name="No Ledger Co",
            phone="+919812345009",
            gstin="29ABCDE1234F1Z5",
            state="Karnataka",
            ledger="",
        ),
    ]

    with pytest.raises(VoucherError, match="party ledger"):
        await _prepare(store, storage, order.order_id)
    assert storage.blobs == {}


async def test_unmapped_product_stock_item_blocks_generation():
    store = _store()
    storage = _storage()
    unmapped = type(PRODUCTS[0])(
        id="p_unmapped",
        name="Unmapped Acid",
        grade="50%",
        unit="kg",
        aliases=["unmapped acid"],
        current_price=10.0,
        stock_item="UNMAPPED",
    )
    store.products = [*PRODUCTS, unmapped]
    order = await _approved(
        store, items=[OrderItem(product="unmapped acid", quantity=10, unit="kg")]
    )

    with pytest.raises(VoucherError, match="stock item"):
        await _prepare(store, storage, order.order_id)
    assert storage.blobs == {}


async def test_uncataloged_product_blocks_generation():
    # A product that was cataloged at intake can drop out of the catalog
    # before generation; the voucher must block rather than emit a broken one.
    store = _store()
    storage = _storage()
    order = await _approved(store)
    store.products = []

    with pytest.raises(VoucherError, match="not mapped"):
        await _prepare(store, storage, order.order_id)
    assert storage.blobs == {}


async def test_missing_gst_ledger_config_blocks_generation():
    store = InMemoryOrderStore(config={**CONFIG, "gst_igst_ledger": ""})
    storage = _storage()
    order = await _approved(store)

    with pytest.raises(VoucherError, match="gst_igst_ledger"):
        await _prepare(store, storage, order.order_id)


async def test_missing_seller_state_config_blocks_generation():
    store = InMemoryOrderStore(config={**CONFIG, "seller_state": ""})
    storage = _storage()
    order = await _approved(store)

    with pytest.raises(VoucherError, match="seller_state"):
        await _prepare(store, storage, order.order_id)


async def test_non_approved_order_blocks_generation():
    store = _store()
    storage = _storage()
    core = OrderProcessingCore(store)
    decision = await core.process(
        Order(
            phone="+919999999999",
            customer=None,
            items=[OrderItem(product="sulfuric acid", quantity=2000, unit="kg")],
            confidence=0.9,
        )
    )
    assert not decision.approved
    order = await store.get_order(decision.order_id)
    assert order is not None

    with pytest.raises(VoucherError, match="not approved"):
        await _prepare(store, storage, decision.order_id)


async def test_voucher_is_not_generated_twice():
    store = _store()
    storage = _storage()
    order = await _approved(store)
    await _prepare(store, storage, order.order_id)

    with pytest.raises(VoucherError, match="already"):
        await _prepare(store, storage, order.order_id)
    assert len(storage.blobs) == 1


async def test_voucher_is_stored_and_order_is_stamped_with_event():
    store = _store()
    storage = _storage()
    order = await _approved(store)

    voucher = await _prepare(store, storage, order.order_id)

    stored = await store.get_order(order.order_id)
    assert stored.voucher_id == voucher.voucher_id
    assert storage.blobs[voucher.voucher_id] == build_voucher_xml(voucher)
    assert any(
        e.event_type == EVENT_VOUCHER_READY and e.order_id == order.order_id
        for e in store.events
    )


async def test_xml_references_only_mapped_masters_never_chat_text():
    # The customer typed an alias ("h2so4"); the voucher must reference the
    # mapped stock item and never the raw chat text.
    store = _store()
    storage = _storage()
    order = await _approved(
        store, items=[OrderItem(product="h2so4", quantity=2000, unit="kg")]
    )

    voucher = await _prepare(store, storage, order.order_id)
    xml = build_voucher_xml(voucher)

    assert "Sulfuric Acid 98%" in xml
    assert "h2so4" not in xml
    assert "sulfuric acid" not in xml


async def test_xml_escapes_master_names():
    from src.voucher import build_voucher_xml

    store = InMemoryOrderStore(config={**CONFIG, "gst_cgst_ledger": "CGST & OUTPUT"})
    storage = InMemoryVoucherStore()
    order = await _approved(store)
    voucher = await _prepare(store, storage, order.order_id)
    xml = build_voucher_xml(voucher)
    assert "CGST &amp; OUTPUT" in xml
    assert "CGST & OUTPUT" not in xml


async def test_gcs_store_uses_blob_name_and_content_type(monkeypatch):
    class FakeBlob:
        def __init__(self, name):
            self.name = name
            self.content = None

        def upload_from_string(self, content, content_type):
            self.content = content
            self.content_type = content_type

        def exists(self):
            return self.content is not None

        def download_as_string(self):
            return self.content.encode()

    class FakeBucket:
        def __init__(self):
            self.blobs = {}

        def blob(self, name):
            return self.blobs.setdefault(name, FakeBlob(name))

    store = GcsVoucherStore("valence-vouchers")
    bucket = FakeBucket()
    store._client = type("Client", (), {"bucket": lambda self, name: bucket})()

    await store.write("vou_ord_x", "<xml/>")
    assert bucket.blobs["vou_ord_x.xml"].content == "<xml/>"
    assert bucket.blobs["vou_ord_x.xml"].content_type == "application/xml"
    assert await store.read("vou_ord_x") == "<xml/>"
    assert await store.read("missing") is None
