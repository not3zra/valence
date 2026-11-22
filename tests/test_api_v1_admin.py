"""Admin API: catalogue import, config updates, integration health (issue #58)."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from src import seed_data
from src.auth import AuthService, InMemoryAuthStore
from src.core import OrderProcessingCore
from src.orders import EscalationReason, Order, OrderItem
from src.store import InMemoryOrderStore
from src.web import create_app

from .fakes import FakeRuntime


def _client():
    store = InMemoryOrderStore(
        customers=[],
        products=[],
        routes=[],
        delivery_locations=[],
        approvers=[],
        config=dict(seed_data.CONFIG),
    )
    auth = AuthService(InMemoryAuthStore())
    admin_key = asyncio.run(auth.issue_admin_key(name="admin")).token
    viewer_key = asyncio.run(auth.issue_key(name="viewer", role="viewer")).token
    client = TestClient(
        create_app(runtime=FakeRuntime(store=store), auth=auth)
    )
    return client, store, admin_key, viewer_key


def _headers(token: str):
    return {"Authorization": f"Bearer {token}"}


def test_catalog_read_and_bulk_upsert_enforce_roles():
    client, store, admin, viewer = _client()

    assert client.get("/api/v1/catalog/customers", headers=_headers(admin)).json() == []
    assert (
        client.put(
            "/api/v1/catalog/customers",
            json=[],
            headers=_headers(viewer),
        ).status_code
        == 403
    )
    customer = {
        "id": "c_new",
        "name": "New Customer",
        "phone": "+919812345099",
        "gstin": "29ABCDE1234F9Z9",
        "state": "Karnataka",
        "ledger": "NEW CUSTOMER",
    }
    response = client.put(
        "/api/v1/catalog/customers", json=[customer], headers=_headers(admin)
    )
    assert response.status_code == 200
    assert response.json() == {"kind": "customers", "upserted": 1}
    listing = client.get("/api/v1/catalog/customers", headers=_headers(viewer))
    assert listing.status_code == 200
    assert [item["id"] for item in listing.json()] == ["c_new"]


def test_catalog_import_validates_before_writing():
    client, store, admin, _ = _client()

    assert (
        client.put(
            "/api/v1/catalog/customers", json=[{"id": "bad"}], headers=_headers(admin)
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/api/v1/catalog/orders", json=[], headers=_headers(admin)
        ).status_code
        == 422
    )
    assert client.get("/api/v1/catalog/customers", headers=_headers(admin)).json() == []


def test_patch_instance_updates_profile_config_and_is_visible_to_readers():
    client, store, admin, viewer = _client()

    response = client.patch(
        "/api/v1/instance",
        json={
            "business_name": "Acme Chemicals",
            "timezone": "Asia/Kolkata",
            "config": {"value_cap_inr": 250000, "cutoff_time": "18:15"},
        },
        headers=_headers(admin),
    )
    assert response.status_code == 200
    assert response.json()["config"]["cutoff_time"] == "18:15"

    body = client.get("/api/v1/instance", headers=_headers(viewer)).json()
    assert body["business_name"] == "Acme Chemicals"
    assert body["timezone"] == "Asia/Kolkata"
    assert body["config"]["value_cap_inr"] == 250000
    assert body["role"] == "viewer"
    assert (
        client.patch(
            "/api/v1/instance", json={"business_name": "Nope"}, headers=_headers(viewer)
        ).status_code
        == 403
    )
    assert (
        client.patch(
            "/api/v1/instance", json={"config": {"nope": 1}}, headers=_headers(admin)
        ).status_code
        == 422
    )


def test_config_update_takes_effect_on_the_next_order_without_a_restart():
    client, store, admin, _ = _client()
    for kind, document in (
        ("customers", seed_data.CUSTOMERS[0]),
        ("products", seed_data.PRODUCTS[0]),
    ):
        from dataclasses import asdict

        assert (
            client.put(
                f"/api/v1/catalog/{kind}",
                json=[asdict(document)],
                headers=_headers(admin),
            ).status_code
            == 200
        )
    assert (
        client.patch(
            "/api/v1/instance",
            json={"config": {"value_cap_inr": 1}},
            headers=_headers(admin),
        ).status_code
        == 200
    )

    decision = asyncio.run(
        OrderProcessingCore(store).process(
            Order(
                phone="+919812345001",
                customer="ChemFab Industries",
                items=[OrderItem(product="sulfuric acid", quantity=10, unit="kg")],
                delivery_location="Peenya Industrial Area",
                confidence=0.9,
            )
        )
    )

    assert not decision.approved
    assert EscalationReason.OVER_VALUE_CAP.value in decision.escalation_reasons


def test_integration_health_is_reported_without_secret_values():
    client, store, admin, viewer = _client()

    assert (
        client.put(
            "/api/v1/integrations/meta",
            json={"access_token": "super-secret"},
            headers=_headers(admin),
        ).status_code
        == 200
    )
    assert (
        client.put(
            "/api/v1/integrations/gemini",
            json={"api_key": ""},
            headers=_headers(admin),
        ).status_code
        == 200
    )
    response = client.get("/api/v1/integrations", headers=_headers(admin))
    assert response.status_code == 200
    report = response.json()
    assert report["meta"] == {"configured": True, "healthy": True}
    assert report["gemini"] == {"configured": True, "healthy": False}
    assert report["tally"] == {"configured": False, "healthy": False}
    assert "super-secret" not in response.text
    instance = client.get("/api/v1/instance", headers=_headers(admin)).json()
    assert instance["integrations"] == report
    assert "super-secret" not in str(instance)
    assert (
        client.get("/api/v1/integrations", headers=_headers(viewer)).status_code == 403
    )
