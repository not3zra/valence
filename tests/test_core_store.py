"""The OrderStore adapter layer.

The Firestore store is pinned against a faked AsyncClient so the exact document
layout — collection, document id, field names — is locked without a project or
an emulator. The in-memory store is the shared test fake the decision engine
and tool tests run against.
"""

from __future__ import annotations

import pytest

from src import seed_data
from src.orders import Order, OrderEvent, OrderItem, OrderStatus
from src.store import FirestoreOrderStore, InMemoryOrderStore


class _Snapshot:
    def __init__(self, data: dict):
        self._data = data

    def to_dict(self) -> dict:
        return self._data


class _DocumentReference:
    def __init__(self, client, collection: str, doc_id: str):
        self._client = client
        self._collection = collection
        self._doc_id = doc_id

    async def get(self):
        data = self._client.docs.get((self._collection, self._doc_id))
        if data is None:
            return _Snapshot({})
        return _Snapshot(data)

    async def set(self, data: dict) -> None:
        self._client.docs[(self._collection, self._doc_id)] = data


class _Collection:
    def __init__(self, client, name: str):
        self._client = client
        self._name = name

    def document(self, doc_id: str) -> _DocumentReference:
        return _DocumentReference(self._client, self._name, doc_id)

    async def stream(self):
        for (collection, _doc_id), data in self._client.docs.items():
            if collection == self._name:
                yield _Snapshot(data)


class _Client:
    def __init__(self):
        self.docs: dict[tuple[str, str], dict] = {}

    def collection(self, name: str) -> _Collection:
        return _Collection(self, name)


@pytest.fixture
def fake_client():
    client = _Client()
    for customer in seed_data.CUSTOMERS:
        client.docs[("customers", customer.id)] = {
            "id": customer.id,
            **{k: v for k, v in vars(customer).items() if k != "id"},
        }
    for product in seed_data.PRODUCTS:
        client.docs[("products", product.id)] = {
            "id": product.id,
            **{k: v for k, v in vars(product).items() if k != "id"},
        }
    client.docs[("config", "order_processing")] = {
        "id": "order_processing",
        **seed_data.CONFIG,
    }
    return client


async def test_firestore_store_reads_seeded_customers(fake_client):
    store = FirestoreOrderStore(fake_client)
    customers = await store.get_customers()
    assert [c.phone for c in customers] == [c.phone for c in seed_data.CUSTOMERS]


async def test_firestore_store_reads_seeded_products(fake_client):
    store = FirestoreOrderStore(fake_client)
    products = await store.get_products()
    assert [p.id for p in products] == [p.id for p in seed_data.PRODUCTS]


async def test_firestore_store_reads_config_from_order_processing_document(fake_client):
    store = FirestoreOrderStore(fake_client)
    config = await store.get_config()
    assert config["value_cap_inr"] == seed_data.CONFIG["value_cap_inr"]
    assert config["min_confidence"] == seed_data.CONFIG["min_confidence"]


async def test_firestore_store_creates_order_document(fake_client):
    store = FirestoreOrderStore(fake_client)
    order = Order(
        order_id="ord_test",
        phone="+919812345001",
        customer="ChemFab Industries",
        customer_id="c_chemfab",
        items=[OrderItem(product="p_sulfuric98", quantity=2000, unit="kg")],
        delivery_location="Peenya",
        delivery_location_id="dl_peenya",
        confidence=0.9,
        status=OrderStatus.APPROVED,
        draft_value_inr=35000.0,
        source_channel="whatsapp",
        source_language="hi",
    )

    await store.create_order(order)

    stored = fake_client.docs[("orders", "ord_test")]
    assert stored["id"] == "ord_test"
    assert stored["status"] == OrderStatus.APPROVED.value
    assert stored["customer_id"] == "c_chemfab"
    assert stored["items"] == [
        {
            "product": "p_sulfuric98",
            "quantity": 2000.0,
            "unit": "kg",
            "rate_inr": None,
        }
    ]


async def test_firestore_store_appends_order_event_document(fake_client):
    store = FirestoreOrderStore(fake_client)
    event = OrderEvent(
        event_id="evt_1",
        order_id="ord_test",
        event_type="order_created",
        payload={"order_id": "ord_test"},
        created_at="2026-01-01T00:00:00+00:00",
    )

    await store.append_order_event(event)

    stored = fake_client.docs[("order_events", "evt_1")]
    assert stored["order_id"] == "ord_test"
    assert stored["event_type"] == "order_created"
    assert stored["payload"] == {"order_id": "ord_test"}


def test_in_memory_store_exposes_seed_data():
    store = InMemoryOrderStore()
    assert store.config["value_cap_inr"] == seed_data.CONFIG["value_cap_inr"]
    assert {c.phone for c in store.customers} == {c.phone for c in seed_data.CUSTOMERS}
    assert [p.id for p in store.products] == [p.id for p in seed_data.PRODUCTS]


async def test_in_memory_store_records_orders_and_events():
    store = InMemoryOrderStore()
    order = Order(
        order_id="ord_1",
        phone="+919812345001",
        items=[],
        status=OrderStatus.PENDING_REVIEW,
    )
    await store.create_order(order)
    await store.append_order_event(
        OrderEvent(
            event_id="evt_1",
            order_id="ord_1",
            event_type="order_created",
            payload={},
            created_at="x",
        )
    )

    assert [o.order_id for o in store.orders] == ["ord_1"]
    assert [e.event_type for e in store.events] == ["order_created"]
