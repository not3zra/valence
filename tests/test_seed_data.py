"""Invariants of the canonical seed dataset.

These assert on the external shape the approval engine, GST computation and
voucher builder will rely on later, so any drift in the seed data is caught
before it silently corrupts downstream behaviour.
"""

from __future__ import annotations

import re

from src import seed_data

E164 = re.compile(r"^\+[1-9]\d{1,14}$")
GSTIN = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z]\d[Z][A-Z0-9]$")
PRODUCT_IDS = {p.id for p in seed_data.PRODUCTS}
ROUTE_IDS = {r.id for r in seed_data.ROUTES}


def test_customers_are_phone_verified():
    assert seed_data.CUSTOMERS, "must seed at least one customer"
    for customer in seed_data.CUSTOMERS:
        assert E164.match(customer.phone), f"{customer.id}: phone not E.164"
        assert GSTIN.match(customer.gstin), f"{customer.id}: invalid GSTIN"
        assert customer.ledger, f"{customer.id}: missing Tally ledger"


def test_products_have_aliases_and_tally_stock_items():
    assert seed_data.PRODUCTS, "must seed at least one product"
    for product in seed_data.PRODUCTS:
        assert product.aliases, f"{product.id}: no aliases"
        assert product.unit, f"{product.id}: no unit"
        assert product.current_price > 0, f"{product.id}: no current price"
        assert product.stock_item, f"{product.id}: missing Tally stock item"


def test_customer_rate_maps_only_reference_seeded_products():
    for customer in seed_data.CUSTOMERS:
        for product_id in customer.agreed_rates:
            assert product_id in PRODUCT_IDS, (
                f"{customer.id} agrees a rate for unknown product {product_id}"
            )
        for product_id in customer.max_quantities:
            assert product_id in PRODUCT_IDS, (
                f"{customer.id} has a 90-day max for unknown product {product_id}"
            )


def test_delivery_locations_reference_seeded_routes():
    assert seed_data.DELIVERY_LOCATIONS, "must seed at least one delivery location"
    for location in seed_data.DELIVERY_LOCATIONS:
        assert location.route_id in ROUTE_IDS, (
            f"{location.id} references unknown route {location.route_id}"
        )
        assert location.state, f"{location.id}: missing state"


def test_approvers_are_allowlisted_phone_numbers():
    assert seed_data.APPROVERS, "must seed at least one approver"
    for approver in seed_data.APPROVERS:
        assert E164.match(approver.phone), f"{approver.id}: phone not E.164"


def test_config_thresholds_are_present_and_sane():
    required = [
        "value_cap_inr",
        "min_confidence",
        "quantity_deviation_above_pct",
        "rate_deviation_pct",
        "dedup_window_minutes",
        "clarify_timeout_hours",
        "clarify_turn_cap",
        "cutoff_time",
        "dispatch_whatsapp_number",
    ]
    for key in required:
        assert key in seed_data.CONFIG, f"config missing {key}"
    assert seed_data.CONFIG["min_confidence"] <= 1.0
    assert seed_data.CONFIG["clarify_turn_cap"] >= 2
