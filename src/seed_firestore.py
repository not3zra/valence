"""Write the canonical seed data into Firestore.

Targets the ``(default)`` database of whatever ``google.cloud.firestore``
resolves: the real project when running under ADC, or the local emulator when
``FIRESTORE_EMULATOR_HOST`` is set. Idempotent — re-running overwrites the
seeded documents with the same content.
"""
from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from google.cloud import firestore

from . import seed_data


CONFIG_DOCUMENT = "order_processing"


async def seed_firestore(
    client: firestore.AsyncClient | None = None, *, wipe: bool = False
) -> None:
    """Populate every operational collection. ``wipe`` clears each first.

    ``client`` is injectable so tests can drive this against a fake without a
    running emulator or a GCP project.
    """
    from google.cloud import firestore

    client = client or firestore.AsyncClient()
    if wipe:
        for collection in seed_data.COLLECTIONS:
            await _wipe_collection(client, collection)
    for collection, docs in seed_data.COLLECTIONS.items():
        for doc in docs:
            data = {
                "id": doc.id,
                **{k: v for k, v in vars(doc).items() if k != "id"},
            }
            await client.collection(collection).document(doc.id).set(data)
    await client.collection("config").document(CONFIG_DOCUMENT).set(
        {"id": CONFIG_DOCUMENT, **seed_data.CONFIG}
    )


async def _wipe_collection(client: firestore.AsyncClient, collection: str) -> None:
    async for doc in client.collection(collection).stream():
        await doc.reference.delete()


if __name__ == "__main__":
    asyncio.run(seed_firestore())
