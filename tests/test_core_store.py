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
    def __init__(self, data: dict, reference=None):
        self._data = data
        self._reference = reference

    def to_dict(self) -> dict:
        return self._data

    @property
    def reference(self):
        return self._reference


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

    async def delete(self) -> None:
        self._client.docs.pop((self._collection, self._doc_id), None)


class _Query:
    def __init__(self, client, collection: str, field: str, value):
        self._client = client
        self._collection = collection
        self._field = field
        self._value = value

    async def stream(self):
        for (collection, _doc_id), data in self._client.docs.items():
            if collection == self._collection and data.get(self._field) == self._value:
                yield _Snapshot(
                    data, reference=_DocumentReference(
                        self._client, self._collection, _doc_id
                    )
                )


class _Collection:
    def __init__(self, client, name: str):
        self._client = client
        self._name = name

    def document(self, doc_id: str) -> _DocumentReference:
        return _DocumentReference(self._client, self._name, doc_id)

    def where(self, field: str, op: str, value) -> _Query:
        return _Query(self._client, self._name, field, value)

    async def stream(self):
        for (collection, _doc_id), data in list(self._client.docs.items()):
            if collection == self._name:
                yield _Snapshot(
                    data, reference=_DocumentReference(
                        self._client, self._name, _doc_id
                    )
                )


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
    for approver in seed_data.APPROVERS:
        client.docs[("approvers", approver.id)] = {
            "id": approver.id,
            **{k: v for k, v in vars(approver).items() if k != "id"},
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


async def test_firestore_store_lists_orders_for_a_phone(fake_client):
    store = FirestoreOrderStore(fake_client)
    await store.create_order(
        Order(
            order_id="ord_chemfab",
            phone="+919812345001",
            items=[OrderItem(product="p_sulfuric98", quantity=2000, unit="kg")],
            status=OrderStatus.APPROVED,
            created_at="2026-08-13T12:00:00+00:00",
        )
    )
    await store.create_order(
        Order(
            order_id="ord_other",
            phone="+919812345002",
            items=[OrderItem(product="p_sulfuric98", quantity=500, unit="kg")],
            status=OrderStatus.APPROVED,
            created_at="2026-08-13T12:05:00+00:00",
        )
    )

    orders = await store.list_orders(phone="+919812345001")

    assert [o.order_id for o in orders] == ["ord_chemfab"]
    assert orders[0].items[0].quantity == 2000.0
    assert orders[0].status is OrderStatus.APPROVED


async def test_firestore_store_lists_all_orders(fake_client):
    store = FirestoreOrderStore(fake_client)
    await store.create_order(
        Order(
            order_id="ord_a",
            phone="+919812345001",
            items=[],
            status=OrderStatus.APPROVED,
        )
    )
    await store.create_order(
        Order(
            order_id="ord_b",
            phone="+919812345002",
            items=[],
            status=OrderStatus.PENDING_REVIEW,
        )
    )

    orders = await store.list_all_orders()

    assert {o.order_id for o in orders} == {"ord_a", "ord_b"}


async def test_firestore_store_lists_order_events_for_an_order(fake_client):
    store = FirestoreOrderStore(fake_client)
    await store.append_order_event(
        OrderEvent(
            event_id="evt_a1",
            order_id="ord_a",
            event_type="order_created",
            payload={"order_id": "ord_a"},
            created_at="2026-08-13T12:00:00+00:00",
        )
    )
    await store.append_order_event(
        OrderEvent(
            event_id="evt_b1",
            order_id="ord_b",
            event_type="order_created",
            payload={"order_id": "ord_b"},
            created_at="2026-08-13T12:01:00+00:00",
        )
    )

    events = await store.list_order_events("ord_a")

    assert [e.event_id for e in events] == ["evt_a1"]
    assert [e.event_type for e in events] == ["order_created"]


async def test_in_memory_store_lists_all_orders():
    store = InMemoryOrderStore()
    await store.create_order(
        Order(order_id="ord_a", phone="+919812345001", items=[])
    )
    await store.create_order(
        Order(order_id="ord_b", phone="+919812345002", items=[])
    )

    orders = await store.list_all_orders()

    assert {o.order_id for o in orders} == {"ord_a", "ord_b"}


async def test_in_memory_store_lists_order_events_for_an_order():
    store = InMemoryOrderStore()
    await store.append_order_event(
        OrderEvent(
            event_id="evt_a",
            order_id="ord_a",
            event_type="order_created",
            payload={},
        )
    )
    await store.append_order_event(
        OrderEvent(
            event_id="evt_b",
            order_id="ord_b",
            event_type="order_created",
            payload={},
        )
    )

    events = await store.list_order_events("ord_a")

    assert [e.event_id for e in events] == ["evt_a"]


def test_order_event_round_trips_through_from_dict():
    event = OrderEvent(
        event_id="evt_1",
        order_id="ord_a",
        event_type="order_escalated",
        payload={"reasons": ["unknown_customer"]},
        created_at="2026-08-13T12:00:00+00:00",
    )

    rebuilt = OrderEvent.from_dict(event.to_dict())

    assert rebuilt.event_id == "evt_1"
    assert rebuilt.order_id == "ord_a"
    assert rebuilt.event_type == "order_escalated"
    assert rebuilt.payload == {"reasons": ["unknown_customer"]}
    assert rebuilt.created_at == "2026-08-13T12:00:00+00:00"


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


async def test_firestore_store_reads_approvers(fake_client):
    store = FirestoreOrderStore(fake_client)
    approvers = await store.get_approvers()
    assert [a.phone for a in approvers] == [a.phone for a in seed_data.APPROVERS]


async def test_firestore_store_gets_an_order_by_id(fake_client):
    store = FirestoreOrderStore(fake_client)
    await store.create_order(
        Order(
            order_id="ord_get",
            phone="+919812345001",
            items=[OrderItem(product="p_sulfuric98", quantity=2000, unit="kg")],
            status=OrderStatus.PENDING_REVIEW,
        )
    )

    order = await store.get_order("ord_get")
    assert order is not None
    assert order.order_id == "ord_get"
    assert order.status is OrderStatus.PENDING_REVIEW


async def test_firestore_store_get_missing_order_returns_none(fake_client):
    store = FirestoreOrderStore(fake_client)
    assert await store.get_order("ord_missing") is None


async def test_firestore_store_updates_order_status(fake_client):
    store = FirestoreOrderStore(fake_client)
    order = Order(
        order_id="ord_upd",
        phone="+919812345001",
        items=[OrderItem(product="p_sulfuric98", quantity=2000, unit="kg")],
        status=OrderStatus.PENDING_REVIEW,
    )
    await store.create_order(order)
    order.status = OrderStatus.APPROVED
    await store.update_order(order)

    stored = fake_client.docs[("orders", "ord_upd")]
    assert stored["status"] == OrderStatus.APPROVED.value


async def test_firestore_store_pending_approval_round_trip(fake_client):
    store = FirestoreOrderStore(fake_client)
    assert await store.get_pending_approval("+919845000001") is None

    await store.set_pending_approval("+919845000001", "ord_approve")
    assert await store.get_pending_approval("+919845000001") == "ord_approve"

    await store.clear_pending_approval("+919845000001")
    assert await store.get_pending_approval("+919845000001") is None


async def test_firestore_store_clears_pending_for_all_approvers_of_order(
    fake_client,
):
    store = FirestoreOrderStore(fake_client)
    await store.set_pending_approval("+919845000001", "ord_approve")
    await store.set_pending_approval("+919845000002", "ord_approve")
    await store.set_pending_approval("+919845000003", "ord_other")

    await store.clear_pending_approvals_for_order("ord_approve")

    assert await store.get_pending_approval("+919845000001") is None
    assert await store.get_pending_approval("+919845000002") is None
    # An unrelated pending order is untouched.
    assert await store.get_pending_approval("+919845000003") == "ord_other"
