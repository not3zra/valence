"""Catalogue/config writes are durable in every OrderStore adapter (issue #58)."""

from __future__ import annotations

from dataclasses import asdict

from src import seed_data
from src.admin import AdminService
from src.sql_store import SqlOrderStore
from src.store import FirestoreOrderStore, InMemoryOrderStore
from tests.test_core_store import _Client


def _url(tmp_path):
    return f"sqlite+aiosqlite:///{tmp_path / 'admin.db'}"


async def test_sql_store_catalog_upsert_survives_a_new_connection(tmp_path):
    url = _url(tmp_path)
    store = SqlOrderStore(url)
    first = asdict(seed_data.CUSTOMERS[0])
    first["name"] = "Renamed"
    new = {
        "id": "c_new",
        "name": "New Customer",
        "phone": "+919812345099",
        "gstin": "29ABCDE1234F9Z9",
        "state": "Karnataka",
        "ledger": "NEW CUSTOMER",
    }
    await store.upsert_catalog("customers", [first, new])
    await store.engine.dispose()

    reopened = SqlOrderStore(url)
    customers = await reopened.get_customers()
    assert len(customers) == len(seed_data.CUSTOMERS) + 1
    assert next(c for c in customers if c.id == first["id"]).name == "Renamed"
    assert any(c.id == new["id"] for c in customers)
    assert [c.id for c in customers[: len(seed_data.CUSTOMERS)]] == [
        c.id for c in seed_data.CUSTOMERS
    ]
    await reopened.engine.dispose()


async def test_sql_store_config_update_survives_a_new_connection(tmp_path):
    url = _url(tmp_path)
    store = SqlOrderStore(url)
    await store.set_config({"value_cap_inr": 450000, "cutoff_time": "20:15"})

    reopened = SqlOrderStore(url)
    config = await reopened.get_config()
    assert config["value_cap_inr"] == 450000
    assert config["cutoff_time"] == "20:15"
    assert config["min_confidence"] == seed_data.CONFIG["min_confidence"]
    await reopened.engine.dispose()


async def test_in_memory_store_upsert_keeps_unlisted_items_and_config_isolated():
    store = InMemoryOrderStore()
    service = AdminService(store)
    original = await service.list_catalog("customers")
    renamed = dict(original[0])
    renamed["name"] = "Renamed"

    await service.upsert_catalog("customers", [renamed])
    await service.update_config({"value_cap_inr": 999})

    after = await service.list_catalog("customers")
    assert len(after) == len(original)
    assert after[0]["name"] == "Renamed"
    assert seed_data.CONFIG["value_cap_inr"] != 999
    assert InMemoryOrderStore().config == seed_data.CONFIG


async def test_firestore_store_writes_catalog_and_merges_config():
    client = _Client()
    store = FirestoreOrderStore(client)
    item = {"id": "r_new", "name": "New Route"}
    await store.upsert_catalog("routes", [item])
    await store.set_config({"cutoff_time": "09:00"})

    assert client.docs[("routes", "r_new")] == item
    assert client.docs[("config", "order_processing")]["cutoff_time"] == "09:00"
    assert "value_cap_inr" in await store.get_config()
