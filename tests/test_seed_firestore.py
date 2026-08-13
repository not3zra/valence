"""Schema contract of the Firestore seed writer.

The Firestore AsyncClient is faked so the exact document layout — collection,
document id, field names — is pinned down without a project or an emulator.
"""

from __future__ import annotations

import pytest

from src import seed_data, seed_firestore


class _DocumentReference:
    def __init__(self, store, collection, doc_id):
        self._store = store
        self._collection = collection
        self._doc_id = doc_id
        self.set_calls = []

    async def set(self, data) -> None:
        self.set_calls.append(data)
        self._store.sets.append((self._collection, self._doc_id, data))

    async def delete(self) -> None:
        self._store.deleted.append((self._collection, self._doc_id))


class _Snapshot:
    def __init__(self, doc_ref):
        self.reference = doc_ref


class _Collection:
    def __init__(self, store, name):
        self._store = store
        self._name = name
        self._docs = {}
        self._streamed = []

    def document(self, doc_id) -> _DocumentReference:
        if doc_id not in self._docs:
            self._docs[doc_id] = _DocumentReference(self._store, self._name, doc_id)
        return self._docs[doc_id]

    async def stream(self):
        for doc_id in self._streamed:
            yield _Snapshot(self.document(doc_id))


class _Client:
    def __init__(self):
        self.collections = {}
        self.sets = []
        self.deleted = []

    def collection(self, name) -> _Collection:
        if name not in self.collections:
            self.collections[name] = _Collection(self, name)
        return self.collections[name]


@pytest.fixture
def fake_client():
    return _Client()


async def test_seed_writes_every_collection_with_expected_document_ids(fake_client):
    await seed_firestore.seed_firestore(fake_client)

    written = {(collection, doc_id) for collection, doc_id, _ in fake_client.sets}
    expected = {
        (collection, doc.id)
        for collection, docs in seed_data.COLLECTIONS.items()
        for doc in docs
    }
    expected.add(("config", seed_firestore.CONFIG_DOCUMENT))
    assert expected == written


async def test_seed_writes_config_document(fake_client):
    await seed_firestore.seed_firestore(fake_client)

    _, _, payload = next(
        (c, d, p) for c, d, p in fake_client.sets if c == "config"
    )
    assert payload["id"] == seed_firestore.CONFIG_DOCUMENT
    assert payload["value_cap_inr"] == seed_data.CONFIG["value_cap_inr"]
    assert "min_confidence" in payload


async def test_seed_writes_customer_identity_fields(fake_client):
    await seed_firestore.seed_firestore(fake_client)

    first = seed_data.CUSTOMERS[0]
    _, _, payload = next(
        (c, d, p)
        for c, d, p in fake_client.sets
        if c == "customers" and d == first.id
    )
    assert payload["phone"] == first.phone
    assert payload["state"] == first.state
    assert payload["ledger"] == first.ledger
    assert payload["agreed_rates"] == first.agreed_rates
    assert payload["max_quantities"] == first.max_quantities


async def test_seed_writes_product_aliases_and_mapping(fake_client):
    await seed_firestore.seed_firestore(fake_client)

    first = seed_data.PRODUCTS[0]
    _, _, payload = next(
        (c, d, p)
        for c, d, p in fake_client.sets
        if c == "products" and d == first.id
    )
    assert payload["aliases"] == first.aliases
    assert payload["grade"] == first.grade
    assert payload["unit"] == first.unit
    assert payload["current_price"] == first.current_price
    assert payload["stock_item"] == first.stock_item


async def test_seed_with_wipe_deletes_existing_documents_before_writing(fake_client):
    collection = fake_client.collection("customers")
    collection._streamed = ["stale_doc"]

    await seed_firestore.seed_firestore(fake_client, wipe=True)

    assert ("customers", "stale_doc") in fake_client.deleted
    assert any(c == "customers" for c, _, _ in fake_client.sets)
