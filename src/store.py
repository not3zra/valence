"""Data access for the Order Processing Core.

Firestore is the external adapter of the whole system; the core never touches
it directly. ``FirestoreOrderStore`` reads the seeded collections and writes
orders + events; ``InMemoryOrderStore`` is the shared test double (and the
``--memory`` mode of the feed harness). Tests fake the client, never the core
logic.
"""

from __future__ import annotations

from typing import Protocol

from google.cloud import firestore

from . import seed_data
from .orders import Order, OrderEvent
from .seed_firestore import CONFIG_DOCUMENT


class OrderStore(Protocol):
    async def get_config(self) -> dict: ...

    async def get_customers(self) -> list[seed_data.Customer]: ...

    async def get_products(self) -> list[seed_data.Product]: ...

    async def get_delivery_locations(self) -> list[seed_data.DeliveryLocation]: ...

    async def create_order(self, order: Order) -> None: ...

    async def append_order_event(self, event: OrderEvent) -> None: ...


class FirestoreOrderStore:
    """OrderStore backed by Firestore, using whatever project the client resolves."""

    def __init__(self, client: firestore.AsyncClient | None = None) -> None:
        self._client = client or firestore.AsyncClient()

    async def get_config(self) -> dict:
        doc = await self._client.collection("config").document(CONFIG_DOCUMENT).get()
        data = doc.to_dict() or {}
        data.pop("id", None)
        return data

    async def get_customers(self) -> list[seed_data.Customer]:
        customers = []
        async for doc in self._client.collection("customers").stream():
            data = doc.to_dict() or {}
            customers.append(seed_data.Customer(**data))
        return customers

    async def get_products(self) -> list[seed_data.Product]:
        products = []
        async for doc in self._client.collection("products").stream():
            data = doc.to_dict() or {}
            products.append(seed_data.Product(**data))
        return products

    async def get_delivery_locations(self) -> list[seed_data.DeliveryLocation]:
        locations = []
        async for doc in self._client.collection("delivery_locations").stream():
            data = doc.to_dict() or {}
            locations.append(seed_data.DeliveryLocation(**data))
        return locations

    async def create_order(self, order: Order) -> None:
        await self._client.collection("orders").document(order.order_id).set(
            order.to_dict()
        )

    async def append_order_event(self, event: OrderEvent) -> None:
        await self._client.collection("order_events").document(event.event_id).set(
            event.to_dict()
        )


class InMemoryOrderStore:
    """OrderStore double over the seed data, recording orders and events in memory."""

    def __init__(
        self,
        *,
        customers: list[seed_data.Customer] | None = None,
        products: list[seed_data.Product] | None = None,
        delivery_locations: list[seed_data.DeliveryLocation] | None = None,
        config: dict | None = None,
    ) -> None:
        self.customers = (
            customers if customers is not None else seed_data.CUSTOMERS
        )
        self.products = products if products is not None else seed_data.PRODUCTS
        self.delivery_locations = (
            delivery_locations
            if delivery_locations is not None
            else seed_data.DELIVERY_LOCATIONS
        )
        self.config = config if config is not None else seed_data.CONFIG
        self.orders: list[Order] = []
        self.events: list[OrderEvent] = []

    async def get_config(self) -> dict:
        return dict(self.config)

    async def get_customers(self) -> list[seed_data.Customer]:
        return list(self.customers)

    async def get_products(self) -> list[seed_data.Product]:
        return list(self.products)

    async def get_delivery_locations(self) -> list[seed_data.DeliveryLocation]:
        return list(self.delivery_locations)

    async def create_order(self, order: Order) -> None:
        self.orders.append(order)

    async def append_order_event(self, event: OrderEvent) -> None:
        self.events.append(event)
