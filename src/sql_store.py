"""SQL OrderStore adapter: SQLite locally, Postgres in production (ADR-0006).

The order core talks to the ``OrderStore`` protocol only; this module is the
adapter behind it. Orders, events, and pending approvals are the durable rows —
they survive an application restart. The distributor seed is the sample fixture
loaded into a fresh database so a configuration-only boot can take its first
order without a manual import step or a second bootstrap CLI (issue #55).

``NullPool`` keeps each connection bound to the loop that uses it: the runtime's
executor owns one loop for agent turns, while the web layer has its own request
loops, so no connection is shared across loops.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime
from datetime import timezone as dt_timezone
from typing import Any

from sqlalchemy import JSON, ScalarResult, String, func, insert, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import NullPool

from . import seed_data
from .auth import InstanceProfile, KeyRecord, Role
from .orders import Order, OrderEvent, OrderStatus

_CONFIG_KEY = "config"
_INSTANCE_ID = "default"


class Base(DeclarativeBase):
    """Tables owned by the application (ADK owns its own session tables)."""


class OrderRow(Base):
    __tablename__ = "valence_orders"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    phone: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String, index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)


class EventRow(Base):
    __tablename__ = "valence_order_events"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    order_id: Mapped[str] = mapped_column(String, index=True)
    created_at: Mapped[str] = mapped_column(String, index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)


class PendingApprovalRow(Base):
    __tablename__ = "valence_pending_approvals"

    approver_phone: Mapped[str] = mapped_column(String, primary_key=True)
    order_ids: Mapped[list[str]] = mapped_column(JSON)


class CatalogRow(Base):
    __tablename__ = "valence_catalog"

    collection: Mapped[str] = mapped_column(String, primary_key=True)
    item_id: Mapped[str] = mapped_column(String, primary_key=True)
    position: Mapped[int] = mapped_column()
    data: Mapped[dict[str, Any]] = mapped_column(JSON)


class InstanceRow(Base):
    __tablename__ = "valence_instance"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    business_name: Mapped[str] = mapped_column(String)
    timezone: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String)


class ApiKeyRow(Base):
    __tablename__ = "valence_api_keys"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String, index=True)
    phone: Mapped[str | None] = mapped_column(String, nullable=True)
    key_hash: Mapped[str] = mapped_column(String)
    salt: Mapped[str] = mapped_column(String)
    prefix: Mapped[str] = mapped_column(String, index=True)
    created_at: Mapped[str] = mapped_column(String)
    revoked_at: Mapped[str | None] = mapped_column(String, nullable=True)


class IntegrationCredentialRow(Base):
    """Provider secrets, deliberately in a different table from user keys."""

    __tablename__ = "valence_integration_credentials"

    service: Mapped[str] = mapped_column(String, primary_key=True)
    data: Mapped[dict[str, str]] = mapped_column(JSON)
    updated_at: Mapped[str] = mapped_column(String)


def _seed_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "collection": _CONFIG_KEY,
            "item_id": _CONFIG_KEY,
            "position": -1,
            "data": dict(seed_data.CONFIG),
        }
    ]
    for collection, items in seed_data.COLLECTIONS.items():
        for position, item in enumerate(items):
            rows.append(
                {
                    "collection": collection,
                    "item_id": item.id,
                    "position": position,
                    "data": asdict(item),
                }
            )
    return rows


class SqlOrderStore:
    """An ``OrderStore`` backed by any SQLAlchemy async database URL."""

    def __init__(self, url: str) -> None:
        self._engine = create_async_engine(url, poolclass=NullPool)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)
        self._ready = False
        self._lock = asyncio.Lock()

    @property
    def engine(self) -> AsyncEngine:
        """The shared engine the ADK session service uses for the same database."""
        return self._engine

    async def initialize(self) -> None:
        """Create the schema and seed the sample catalogue (``valence init``)."""
        await self._ensure_ready()

    async def _ensure_ready(self) -> None:
        async with self._lock:
            if self._ready:
                return
            async with self._engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
                count = await connection.scalar(
                    select(func.count()).select_from(CatalogRow)
                )
                if not count:
                    await connection.execute(insert(CatalogRow), _seed_rows())
            self._ready = True

    async def _catalog(self, collection: str) -> list[dict[str, Any]]:
        await self._ensure_ready()
        async with self._sessions() as session:
            result: ScalarResult[dict[str, Any]] = await session.scalars(
                select(CatalogRow.data)
                .where(CatalogRow.collection == collection)
                .order_by(CatalogRow.position)
            )
            return list(result.all())

    async def get_config(self) -> dict:
        await self._ensure_ready()
        async with self._sessions() as session:
            row = await session.get(CatalogRow, (_CONFIG_KEY, _CONFIG_KEY))
            return dict(row.data) if row is not None else dict(seed_data.CONFIG)

    async def get_customers(self) -> list[seed_data.Customer]:
        from .seed_data import Customer

        return [Customer(**data) for data in await self._catalog("customers")]

    async def get_products(self) -> list[seed_data.Product]:
        from .seed_data import Product

        return [Product(**data) for data in await self._catalog("products")]

    async def get_delivery_locations(self) -> list[seed_data.DeliveryLocation]:
        from .seed_data import DeliveryLocation

        return [
            DeliveryLocation(**data)
            for data in await self._catalog("delivery_locations")
        ]

    async def get_approvers(self) -> list[seed_data.Approver]:
        from .seed_data import Approver

        return [Approver(**data) for data in await self._catalog("approvers")]

    async def get_routes(self) -> list[seed_data.Route]:
        from .seed_data import Route

        return [Route(**data) for data in await self._catalog("routes")]

    async def create_order(self, order: Order) -> None:
        await self._ensure_ready()
        async with self._sessions() as session, session.begin():
            await session.merge(OrderRow(**_order_row(order)))

    async def get_order(self, order_id: str) -> Order | None:
        await self._ensure_ready()
        async with self._sessions() as session:
            row = await session.get(OrderRow, order_id)
            return Order.from_dict(row.data) if row is not None else None

    async def update_order(self, order: Order) -> None:
        await self.create_order(order)

    async def append_order_event(self, event: OrderEvent) -> None:
        await self._ensure_ready()
        async with self._sessions() as session, session.begin():
            session.add(
                EventRow(
                    id=event.event_id,
                    order_id=event.order_id,
                    created_at=event.created_at,
                    data=event.to_dict(),
                )
            )

    async def list_orders(self, *, phone: str) -> list[Order]:
        await self._ensure_ready()
        async with self._sessions() as session:
            result: ScalarResult[dict[str, Any]] = await session.scalars(
                select(OrderRow.data).where(OrderRow.phone == phone)
            )
            return [Order.from_dict(data) for data in result.all()]

    async def list_all_orders(self) -> list[Order]:
        await self._ensure_ready()
        async with self._sessions() as session:
            result: ScalarResult[dict[str, Any]] = await session.scalars(
                select(OrderRow.data)
            )
            return [Order.from_dict(data) for data in result.all()]

    async def list_approved_orders(self) -> list[Order]:
        await self._ensure_ready()
        async with self._sessions() as session:
            result: ScalarResult[dict[str, Any]] = await session.scalars(
                select(OrderRow.data).where(
                    OrderRow.status == OrderStatus.APPROVED.value
                )
            )
            return [Order.from_dict(data) for data in result.all()]

    async def list_order_events(self, order_id: str) -> list[OrderEvent]:
        await self._ensure_ready()
        async with self._sessions() as session:
            result: ScalarResult[dict[str, Any]] = await session.scalars(
                select(EventRow.data)
                .where(EventRow.order_id == order_id)
                .order_by(EventRow.created_at, EventRow.id)
            )
            return [OrderEvent.from_dict(data) for data in result.all()]

    async def get_pending_approval(self, approver_phone: str) -> str | None:
        order_ids = await self.get_pending_approvals_list(approver_phone)
        return order_ids[0] if order_ids else None

    async def get_pending_approvals_list(self, approver_phone: str) -> list[str]:
        await self._ensure_ready()
        async with self._sessions() as session:
            row = await session.get(PendingApprovalRow, approver_phone)
            return list(row.order_ids) if row is not None else []

    async def set_pending_approval(self, approver_phone: str, order_id: str) -> None:
        await self._ensure_ready()
        async with self._sessions() as session, session.begin():
            row = await session.get(PendingApprovalRow, approver_phone)
            order_ids = list(row.order_ids) if row is not None else []
            if order_id not in order_ids:
                order_ids.append(order_id)
            await session.merge(
                PendingApprovalRow(approver_phone=approver_phone, order_ids=order_ids)
            )

    async def clear_pending_approval(self, approver_phone: str) -> None:
        await self._ensure_ready()
        async with self._sessions() as session, session.begin():
            row = await session.get(PendingApprovalRow, approver_phone)
            if row is not None:
                await session.delete(row)

    async def clear_pending_approvals_for_order(self, order_id: str) -> None:
        await self._ensure_ready()
        async with self._sessions() as session, session.begin():
            rows = (await session.scalars(select(PendingApprovalRow))).all()
            for row in rows:
                if order_id not in row.order_ids:
                    continue
                remaining = [item for item in row.order_ids if item != order_id]
                if remaining:
                    row.order_ids = remaining
                else:
                    await session.delete(row)


def _order_row(order: Order) -> dict[str, Any]:
    data = order.to_dict()
    order_id = str(data["id"])
    if not order.order_id:
        raise ValueError("an order must have a system-generated id before storage")
    return {
        "id": order_id,
        "phone": str(data["phone"]),
        "status": str(data["status"]),
        "data": data,
    }


def _instance_profile(row: InstanceRow) -> InstanceProfile:
    return InstanceProfile(
        business_name=row.business_name,
        timezone=row.timezone,
        created_at=row.created_at,
    )


def _key_record(row: ApiKeyRow) -> KeyRecord:
    return KeyRecord(
        id=row.id,
        name=row.name,
        role=Role(row.role),
        phone=row.phone,
        key_hash=row.key_hash,
        salt=row.salt,
        prefix=row.prefix,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
    )


class SqlAuthStore:
    """SQL adapter for the Instance, API keys, and Integration credentials.

    It can share an existing engine with the order store or open its own from a
    database URL. Schema creation is idempotent so ``valence init`` and the
    Alembic entrypoint both converge on the same tables.
    """

    def __init__(
        self, url: str | None = None, *, engine: AsyncEngine | None = None
    ) -> None:
        if engine is None:
            if not url:
                raise ValueError("SqlAuthStore needs a database URL or an engine")
            engine = create_async_engine(url, poolclass=NullPool)
        self._engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._ready = False
        self._lock = asyncio.Lock()

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    async def init_schema(self) -> None:
        async with self._lock:
            if self._ready:
                return
            async with self._engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            self._ready = True

    async def create_instance(
        self, *, business_name: str, timezone: str
    ) -> InstanceProfile:
        await self.init_schema()
        async with self._sessions() as session, session.begin():
            row = await session.get(InstanceRow, _INSTANCE_ID)
            if row is None:
                row = InstanceRow(
                    id=_INSTANCE_ID,
                    business_name=business_name,
                    timezone=timezone,
                    created_at=datetime.now(dt_timezone.utc).isoformat(),
                )
                session.add(row)
            return _instance_profile(row)

    async def get_instance(self) -> InstanceProfile | None:
        await self.init_schema()
        async with self._sessions() as session:
            row = await session.get(InstanceRow, _INSTANCE_ID)
            if row is None:
                return None
            return _instance_profile(row)

    async def insert_key(self, record: KeyRecord) -> None:
        await self.init_schema()
        async with self._sessions() as session, session.begin():
            session.add(
                ApiKeyRow(
                    id=record.id,
                    name=record.name,
                    role=record.role.value,
                    phone=record.phone,
                    key_hash=record.key_hash,
                    salt=record.salt,
                    prefix=record.prefix,
                    created_at=record.created_at,
                    revoked_at=record.revoked_at,
                )
            )

    async def key_by_prefix(self, prefix: str) -> list[KeyRecord]:
        await self.init_schema()
        async with self._sessions() as session:
            result: ScalarResult[ApiKeyRow] = await session.scalars(
                select(ApiKeyRow).where(ApiKeyRow.prefix == prefix)
            )
            return [_key_record(row) for row in result.all()]

    async def get_key(self, key_id: str) -> KeyRecord | None:
        await self.init_schema()
        async with self._sessions() as session:
            row = await session.get(ApiKeyRow, key_id)
            return _key_record(row) if row is not None else None

    async def list_keys(self) -> list[KeyRecord]:
        await self.init_schema()
        async with self._sessions() as session:
            result: ScalarResult[ApiKeyRow] = await session.scalars(
                select(ApiKeyRow).order_by(ApiKeyRow.created_at, ApiKeyRow.id)
            )
            return [_key_record(row) for row in result.all()]

    async def revoke_key(self, key_id: str, *, revoked_at: str) -> bool:
        await self.init_schema()
        async with self._sessions() as session, session.begin():
            row = await session.get(ApiKeyRow, key_id)
            if row is None or row.revoked_at is not None:
                return False
            row.revoked_at = revoked_at
            return True

    async def set_integration(self, service: str, values: dict[str, str]) -> None:
        await self.init_schema()
        async with self._sessions() as session, session.begin():
            await session.merge(
                IntegrationCredentialRow(
                    service=service,
                    data=dict(values),
                    updated_at=datetime.now(dt_timezone.utc).isoformat(),
                )
            )

    async def get_integration(self, service: str) -> dict[str, str] | None:
        await self.init_schema()
        async with self._sessions() as session:
            row = await session.get(IntegrationCredentialRow, service)
            return dict(row.data) if row is not None else None

    async def list_integrations(self) -> list[str]:
        await self.init_schema()
        async with self._sessions() as session:
            result: ScalarResult[str] = await session.scalars(
                select(IntegrationCredentialRow.service).order_by(
                    IntegrationCredentialRow.service
                )
            )
            return list(result.all())
