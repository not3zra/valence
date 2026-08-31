"""Data access for the Order Processing Core.

Firestore is the external adapter of the whole system; the core never touches
it directly. ``FirestoreOrderStore`` reads the seeded collections and writes
orders + events; ``InMemoryOrderStore`` is the shared test double (and the
``--memory`` mode of the feed harness). Tests fake the client, never the core
logic.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from google.cloud import firestore

from . import seed_data
from .orders import Order, OrderEvent, OrderStatus
from .seed_firestore import CONFIG_DOCUMENT


class OrderStore(Protocol):
    async def get_config(self) -> dict: ...

    async def get_customers(self) -> list[seed_data.Customer]: ...

    async def get_products(self) -> list[seed_data.Product]: ...

    async def get_delivery_locations(self) -> list[seed_data.DeliveryLocation]: ...

    async def get_approvers(self) -> list[seed_data.Approver]: ...

    async def get_routes(self) -> list[seed_data.Route]: ...

    async def create_order(self, order: Order) -> None: ...

    async def get_order(self, order_id: str) -> Order | None: ...

    async def update_order(self, order: Order) -> None: ...

    async def append_order_event(self, event: OrderEvent) -> None: ...

    async def list_orders(self, *, phone: str) -> list[Order]: ...

    async def list_all_orders(self) -> list[Order]: ...

    async def list_approved_orders(self) -> list[Order]: ...

    async def list_order_events(self, order_id: str) -> list[OrderEvent]: ...

    async def get_pending_approval(self, approver_phone: str) -> str | None: ...

    async def get_pending_approvals_list(self, approver_phone: str) -> list[str]: ...

    async def set_pending_approval(
        self, approver_phone: str, order_id: str
    ) -> None: ...

    async def clear_pending_approval(self, approver_phone: str) -> None: ...

    async def clear_pending_approvals_for_order(self, order_id: str) -> None: ...


class FirestoreOrderStore:
    """OrderStore backed by Firestore, using whatever project the client resolves.

    The Firestore ``AsyncClient`` is created lazily on first use so that its
    gRPC channels bind to the *running* event loop (uvicorn's), not the
    import-time loop.
    """

    def __init__(self, client: firestore.AsyncClient | None = None) -> None:
        self._client = client
        self._client_provided = client is not None

    @property
    def client(self) -> firestore.AsyncClient:
        if self._client is None:
            try:
                import google.api_core.grpc_helpers_async as _gh
                if hasattr(_gh, "_channel_cache"):
                    _gh._channel_cache.clear()
            except Exception:
                pass
            self._client = firestore.AsyncClient()
        return self._client

    async def get_config(self) -> dict:
        doc = await self.client.collection("config").document(CONFIG_DOCUMENT).get()
        data = doc.to_dict() or {}
        data.pop("id", None)
        return data

    async def get_customers(self) -> list[seed_data.Customer]:
        customers = []
        async for doc in self.client.collection("customers").stream():
            data = doc.to_dict() or {}
            customers.append(seed_data.Customer(**data))
        return customers

    async def get_products(self) -> list[seed_data.Product]:
        products = []
        async for doc in self.client.collection("products").stream():
            data = doc.to_dict() or {}
            products.append(seed_data.Product(**data))
        return products

    async def get_delivery_locations(self) -> list[seed_data.DeliveryLocation]:
        locations = []
        async for doc in self.client.collection("delivery_locations").stream():
            data = doc.to_dict() or {}
            locations.append(seed_data.DeliveryLocation(**data))
        return locations

    async def get_approvers(self) -> list[seed_data.Approver]:
        approvers = []
        async for doc in self.client.collection("approvers").stream():
            data = doc.to_dict() or {}
            approvers.append(seed_data.Approver(**data))
        return approvers

    async def get_routes(self) -> list[seed_data.Route]:
        routes = []
        async for doc in self.client.collection("routes").stream():
            data = doc.to_dict() or {}
            routes.append(seed_data.Route(**data))
        return routes

    async def create_order(self, order: Order) -> None:
        await self.client.collection("orders").document(order.order_id).set(
            order.to_dict()
        )

    async def get_order(self, order_id: str) -> Order | None:
        doc = await self.client.collection("orders").document(order_id).get()
        data = doc.to_dict() or {}
        if not data:
            return None
        return Order.from_dict(data)

    async def update_order(self, order: Order) -> None:
        await self.client.collection("orders").document(order.order_id).set(
            order.to_dict()
        )

    async def append_order_event(self, event: OrderEvent) -> None:
        await self.client.collection("order_events").document(event.event_id).set(
            event.to_dict()
        )

    async def list_orders(self, *, phone: str) -> list[Order]:
        orders = []
        async for doc in self.client.collection("orders").where(
            "phone", "==", phone
        ).stream():
            data = doc.to_dict() or {}
            orders.append(Order.from_dict(data))
        return orders

    async def list_all_orders(self) -> list[Order]:
        orders = []
        async for doc in self.client.collection("orders").stream():
            data = doc.to_dict() or {}
            orders.append(Order.from_dict(data))
        return orders

    async def list_approved_orders(self) -> list[Order]:
        orders = []
        # Filter to the only status the Loading List ever renders (issue #9):
        # the stream is indexed on status, not a full-collection scan (security
        # #32), so an attacker-triggered render no longer reads every order.
        async for doc in self.client.collection("orders").where(
            "status", "==", OrderStatus.APPROVED.value
        ).stream():
            data = doc.to_dict() or {}
            orders.append(Order.from_dict(data))
        return orders

    async def list_order_events(self, order_id: str) -> list[OrderEvent]:
        events = []
        async for doc in self.client.collection("order_events").where(
            "order_id", "==", order_id
        ).stream():
            data = doc.to_dict() or {}
            events.append(OrderEvent.from_dict(data))
        return events

    async def get_pending_approval(self, approver_phone: str) -> str | None:
        doc = await self.client.collection("pending_approvals").document(
            approver_phone
        ).get()
        data = doc.to_dict() or {}
        order_ids = data.get("order_ids", [])
        return order_ids[0] if order_ids else None

    async def get_pending_approvals_list(self, approver_phone: str) -> list[str]:
        doc = await self.client.collection("pending_approvals").document(
            approver_phone
        ).get()
        data = doc.to_dict() or {}
        return list(data.get("order_ids", []))

    async def set_pending_approval(self, approver_phone: str, order_id: str) -> None:
        doc_ref = self.client.collection("pending_approvals").document(
            approver_phone
        )
        doc = await doc_ref.get()
        data = doc.to_dict() or {}
        order_ids = list(data.get("order_ids", []))
        if order_id not in order_ids:
            order_ids.append(order_id)
        await doc_ref.set({"approver_phone": approver_phone, "order_ids": order_ids})

    async def clear_pending_approval(self, approver_phone: str) -> None:
        await self.client.collection("pending_approvals").document(
            approver_phone
        ).delete()

    async def clear_pending_approvals_for_order(self, order_id: str) -> None:
        pending = self.client.collection("pending_approvals")
        async for doc in pending.stream():
            data = doc.to_dict() or {}
            order_ids = list(data.get("order_ids", []))
            if order_id in order_ids:
                order_ids.remove(order_id)
                if order_ids:
                    await doc.reference.update({"order_ids": order_ids})
                else:
                    await doc.reference.delete()


class ProxiedFirestoreStore:
    """Proxies OrderStore calls through a remote event loop.

    Web routes run on uvicorn's event loop; the agent's ``FirestoreOrderStore``
    has gRPC channels bound to the executor's event loop.  Creating a second
    ``AsyncClient`` doesn't help because gRPC channels are shared via the
    ``_channel_cache``.  This wrapper schedules every call onto the executor's
    loop, where the gRPC client already works, and awaits the result.
    """

    def __init__(self, store: OrderStore, loop: asyncio.AbstractEventLoop) -> None:
        self._store = store
        self._loop = loop

    def _proxy(self, coro):  # type: ignore[no-untyped-def]
        f = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return asyncio.wrap_future(f)

    async def get_config(self) -> dict:
        return await self._proxy(self._store.get_config())

    async def get_customers(self) -> list[seed_data.Customer]:
        return await self._proxy(self._store.get_customers())

    async def get_products(self) -> list[seed_data.Product]:
        return await self._proxy(self._store.get_products())

    async def get_delivery_locations(self) -> list[seed_data.DeliveryLocation]:
        return await self._proxy(self._store.get_delivery_locations())

    async def get_approvers(self) -> list[seed_data.Approver]:
        return await self._proxy(self._store.get_approvers())

    async def get_routes(self) -> list[seed_data.Route]:
        return await self._proxy(self._store.get_routes())

    async def create_order(self, order: Order) -> None:
        return await self._proxy(self._store.create_order(order))

    async def get_order(self, order_id: str) -> Order | None:
        return await self._proxy(self._store.get_order(order_id))

    async def update_order(self, order: Order) -> None:
        return await self._proxy(self._store.update_order(order))

    async def append_order_event(self, event: OrderEvent) -> None:
        return await self._proxy(self._store.append_order_event(event))

    async def list_orders(self, *, phone: str) -> list[Order]:
        return await self._proxy(self._store.list_orders(phone=phone))

    async def list_all_orders(self) -> list[Order]:
        return await self._proxy(self._store.list_all_orders())

    async def list_approved_orders(self) -> list[Order]:
        return await self._proxy(self._store.list_approved_orders())

    async def list_order_events(self, order_id: str) -> list[OrderEvent]:
        return await self._proxy(self._store.list_order_events(order_id))

    async def get_pending_approval(self, approver_phone: str) -> str | None:
        return await self._proxy(self._store.get_pending_approval(approver_phone))

    async def get_pending_approvals_list(self, approver_phone: str) -> list[str]:
        return await self._proxy(
            self._store.get_pending_approvals_list(approver_phone)
        )

    async def set_pending_approval(
        self, approver_phone: str, order_id: str
    ) -> None:
        return await self._proxy(
            self._store.set_pending_approval(approver_phone, order_id)
        )

    async def clear_pending_approval(self, approver_phone: str) -> None:
        return await self._proxy(self._store.clear_pending_approval(approver_phone))

    async def clear_pending_approvals_for_order(self, order_id: str) -> None:
        return await self._proxy(
            self._store.clear_pending_approvals_for_order(order_id)
        )


class InMemoryOrderStore:
    """OrderStore double over the seed data, recording orders and events in memory."""

    def __init__(
        self,
        *,
        customers: list[seed_data.Customer] | None = None,
        products: list[seed_data.Product] | None = None,
        delivery_locations: list[seed_data.DeliveryLocation] | None = None,
        approvers: list[seed_data.Approver] | None = None,
        routes: list[seed_data.Route] | None = None,
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
        self.approvers = approvers if approvers is not None else seed_data.APPROVERS
        self.routes = routes if routes is not None else seed_data.ROUTES
        self.orders: list[Order] = []
        self.events: list[OrderEvent] = []
        self.pending_approvals: dict[str, list[str]] = {}

    async def get_config(self) -> dict:
        return dict(self.config)

    async def get_customers(self) -> list[seed_data.Customer]:
        return list(self.customers)

    async def get_products(self) -> list[seed_data.Product]:
        return list(self.products)

    async def get_delivery_locations(self) -> list[seed_data.DeliveryLocation]:
        return list(self.delivery_locations)

    async def get_approvers(self) -> list[seed_data.Approver]:
        return list(self.approvers)

    async def get_routes(self) -> list[seed_data.Route]:
        return list(self.routes)

    async def create_order(self, order: Order) -> None:
        self.orders.append(order)

    async def get_order(self, order_id: str) -> Order | None:
        for order in self.orders:
            if order.order_id == order_id:
                return order
        return None

    async def update_order(self, order: Order) -> None:
        for i, existing in enumerate(self.orders):
            if existing.order_id == order.order_id:
                self.orders[i] = order
                return
        self.orders.append(order)

    async def append_order_event(self, event: OrderEvent) -> None:
        self.events.append(event)

    async def list_orders(self, *, phone: str) -> list[Order]:
        return [order for order in self.orders if order.phone == phone]

    async def list_all_orders(self) -> list[Order]:
        return list(self.orders)

    async def list_approved_orders(self) -> list[Order]:
        return [
            order for order in self.orders if order.status is OrderStatus.APPROVED
        ]

    async def list_order_events(self, order_id: str) -> list[OrderEvent]:
        return [event for event in self.events if event.order_id == order_id]

    async def get_pending_approval(self, approver_phone: str) -> str | None:
        order_ids = self.pending_approvals.get(approver_phone, [])
        return order_ids[0] if order_ids else None

    async def get_pending_approvals_list(self, approver_phone: str) -> list[str]:
        return list(self.pending_approvals.get(approver_phone, []))

    async def set_pending_approval(self, approver_phone: str, order_id: str) -> None:
        order_ids = self.pending_approvals.setdefault(approver_phone, [])
        if order_id not in order_ids:
            order_ids.append(order_id)

    async def clear_pending_approval(self, approver_phone: str) -> None:
        self.pending_approvals.pop(approver_phone, None)

    async def clear_pending_approvals_for_order(self, order_id: str) -> None:
        for approver_phone, order_ids in list(self.pending_approvals.items()):
            if order_id in order_ids:
                order_ids.remove(order_id)
                if not order_ids:
                    self.pending_approvals.pop(approver_phone, None)
