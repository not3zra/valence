"""Canonical seed data for Firestore.

Every operational collection the agent runs on is seeded here: customers with
verified contact numbers, products with an alias table, routes, delivery
locations, the approver allowlist, and the configurable threshold values.

Identity rules (ADR-0002): a customer is verified by phone-exact match only,
and the canonical ``ledger``/``stock_item`` names are what flows to Tally
(ADR-0003) — never raw chat text.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Customer:
    id: str
    name: str
    phone: str  # E.164, verified — phone-exact match is the only identity path
    gstin: str
    state: str
    ledger: str  # Tally party ledger this customer maps to
    # Product id -> rate in INR per unit agreed with this customer.
    agreed_rates: dict[str, float] = field(default_factory=dict)
    # Product id -> largest quantity ordered by this customer in the last 90 days.
    max_quantities: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Product:
    id: str
    name: str
    grade: str
    unit: str
    aliases: list[str]  # aliases the customer might actually type / say
    current_price: float  # catalog price in INR per unit
    tier_price: float | None = None  # published pricing-tier rate in INR per unit
    gst_pct: float = 18.0  # GST rate on this product + grade, for the voucher
    stock_item: str = "UNMAPPED"  # Tally stock item this product + grade maps to


@dataclass(frozen=True)
class Route:
    id: str
    name: str


@dataclass(frozen=True)
class DeliveryLocation:
    id: str
    name: str
    route_id: str
    address: str
    state: str


@dataclass(frozen=True)
class Approver:
    id: str
    phone: str  # allowlisted number that may decide escalated orders


CUSTOMERS: list[Customer] = [
    Customer(
        id="c_chemfab",
        name="ChemFab Industries",
        phone="+919812345001",
        gstin="29ABCDE1234F1Z5",
        state="Karnataka",
        ledger="CHEMFAB INDUSTRIES",
        agreed_rates={"p_sulfuric98": 17.5, "p_hcl": 9.0},
        max_quantities={"p_sulfuric98": 4000.0, "p_hcl": 2000.0},
    ),
    Customer(
        id="c_maruthi",
        name="Maruthi Coatings",
        phone="+919812345002",
        gstin="27ABCDE1234F1Z8",
        state="Maharashtra",
        ledger="MARUTHI COATINGS",
        agreed_rates={"p_sulfuric98": 18.0, "p_naoh": 37.0},
        max_quantities={"p_sulfuric98": 6000.0, "p_naoh": 3000.0},
    ),
    Customer(
        id="c_swastik",
        name="Swastik Paints",
        phone="+919812345003",
        gstin="33ABCDE1234F1Z6",
        state="Tamil Nadu",
        ledger="SWASTIK PAINTS",
        agreed_rates={"p_toluene": 61.0, "p_xylene": 66.0},
        max_quantities={"p_toluene": 1500.0, "p_xylene": 1200.0},
    ),
    Customer(
        id="c_anand",
        name="Anand Agro Chem",
        phone="+919812345004",
        gstin="29ABCDE1234F1Z2",
        state="Karnataka",
        ledger="ANAND AGRO CHEM",
        agreed_rates={"p_hcl": 9.25},
        max_quantities={"p_hcl": 2500.0},
    ),
]

PRODUCTS: list[Product] = [
    Product(
        id="p_sulfuric98",
        name="Sulfuric Acid",
        grade="98%",
        unit="kg",
        aliases=["sulfuric", "sulphuric acid", "sulphuric", "h2so4", "98% acid"],
        current_price=18.0,
        tier_price=17.75,
        stock_item="SULFURIC ACID 98%",
    ),
    Product(
        id="p_hcl",
        name="Hydrochloric Acid",
        grade="32%",
        unit="kg",
        aliases=["hydrochloric", "hcl", "muriatic acid", "32% hcl"],
        current_price=9.5,
        tier_price=9.25,
        stock_item="HYDROCHLORIC ACID 32%",
    ),
    Product(
        id="p_naoh",
        name="Caustic Soda Lye",
        grade="48%",
        unit="kg",
        aliases=["caustic soda", "caustic", "naoh", "lye 48%"],
        current_price=38.0,
        stock_item="CAUSTIC SODA LYE 48%",
    ),
    Product(
        id="p_toluene",
        name="Toluene",
        grade="99%",
        unit="L",
        aliases=["toluene", "methylbenzene", "c7h8"],
        current_price=62.0,
        stock_item="TOLUENE 99%",
    ),
    Product(
        id="p_xylene",
        name="Xylene",
        grade="99%",
        unit="L",
        aliases=["xylene", "dimethylbenzene", "c8h10"],
        current_price=67.0,
        stock_item="XYLENE 99%",
    ),
]

ROUTES: list[Route] = [
    Route(id="r_bengaluru_east", name="Bengaluru East"),
    Route(id="r_bengaluru_west", name="Bengaluru West"),
    Route(id="r_mysuru", name="Mysuru"),
]

DELIVERY_LOCATIONS: list[DeliveryLocation] = [
    DeliveryLocation(
        id="dl_peenya",
        name="Peenya Industrial Area",
        route_id="r_bengaluru_west",
        address="14th Cross, Peenya 2nd Stage, Bengaluru",
        state="Karnataka",
    ),
    DeliveryLocation(
        id="dl_whitefield",
        name="Whitefield",
        route_id="r_bengaluru_east",
        address="EPIP Zone, Whitefield, Bengaluru",
        state="Karnataka",
    ),
    DeliveryLocation(
        id="dl_bommasandra",
        name="Bommasandra Industrial Area",
        route_id="r_bengaluru_east",
        address="Hosur Road, Bommasandra, Bengaluru",
        state="Karnataka",
    ),
    DeliveryLocation(
        id="dl_mysuru",
        name="Mysuru MIDC",
        route_id="r_mysuru",
        address="Metagalli Industrial Area, Mysuru",
        state="Karnataka",
    ),
]

APPROVERS: list[Approver] = [
    Approver(id="a_nikhil", phone="+919845000001"),
    Approver(id="a_priya", phone="+919845000002"),
]

# Firestore-configurable thresholds (ADR-0002): business judgment lives in the
# database, never hardcoded in the approval engine.
CONFIG: dict[str, object] = {
    "value_cap_inr": 100000,  # estimated value above this escalates
    "min_confidence": 0.7,  # extraction confidence below this escalates
    "quantity_deviation_above_pct": 0.5,  # qty > 50% above 90-day max is an anomaly
    "rate_deviation_pct": 0.2,  # estimated rate > 20% off agreed rate is an anomaly
    # same sender + first item qty within window -> duplicate
    "dedup_window_minutes": 30,
    "clarify_timeout_hours": 24,  # no reply -> partial order promotes to escalation
    "clarify_turn_cap": 3,  # clarify loop hands off to escalation after N turns
    "cutoff_time": "17:30",  # daily cutoff for a delivery day's Loading List
    "business_timezone": "Asia/Kolkata",  # cutoff and delivery day resolve here
    "dispatch_whatsapp_number": "+919845000003",  # late-order heads-up channel
    "currency": "INR",
    # Seller identity for the GST split on the Tally voucher (issue #8): the
    # delivery-location state (falling back to the customer state, then the
    # customer's GSTIN) compared against ``seller_state`` decides CGST+SGST
    # (intra-state) vs IGST.
    "seller_state": "Karnataka",
    # Mapped Tally GST duty ledgers — the voucher only references these names.
    "gst_cgst_ledger": "CGST OUTPUT",
    "gst_sgst_ledger": "SGST OUTPUT",
    "gst_igst_ledger": "IGST OUTPUT",
    # Revenue ledger the inventory entries' sales allocations reference.
    "gst_sales_ledger": "SALES",
}

SeededDoc = Customer | Product | Route | DeliveryLocation | Approver

COLLECTIONS: dict[str, Sequence[SeededDoc]] = {
    "customers": CUSTOMERS,
    "products": PRODUCTS,
    "routes": ROUTES,
    "delivery_locations": DELIVERY_LOCATIONS,
    "approvers": APPROVERS,
}
