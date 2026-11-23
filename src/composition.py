"""Shared composition helpers for the REST app and the MCP transports.

One Instance resolves its adapters and services the same way whether a caller
arrives over REST, the hosted ``/mcp`` mount, or the standalone MCP CLI, so
swapping an adapter stays a one-module change.
"""

from __future__ import annotations

from dataclasses import dataclass

from .admin import AdminService
from .auth import ApproverPhones, AuthService
from .config import settings
from .core import OrderProcessingCore
from .intake import IntakeService
from .loading_service import LoadingService
from .order_actions import OrderActionService
from .sql_store import SqlAuthStore, SqlOrderStore
from .voucher import VoucherStore


@dataclass(frozen=True)
class AgentServices:
    """The service layer both transports expose."""

    admin: AdminService | None
    intake: IntakeService | None
    orders: OrderActionService | None
    loading: LoadingService | None


def approver_phones(runtime) -> ApproverPhones:
    async def phones() -> list[str]:
        store = runtime.store
        if store is None:
            return []
        return [approver.phone for approver in await store.get_approvers()]

    return phones


def auth_store(runtime) -> SqlAuthStore:
    store = runtime.store
    if isinstance(store, SqlOrderStore):
        return SqlAuthStore(engine=store.engine)
    return SqlAuthStore(settings.database_url)


def build_auth(runtime) -> AuthService:
    """Build the one authorization service REST and MCP both call."""
    return AuthService(
        auth_store(runtime),
        setup_token=settings.setup_token,
        approver_phones=approver_phones(runtime),
    )


def build_agent_services(
    runtime,
    storage: VoucherStore,
    tally_push,
    *,
    notifier=None,
    turn_timeout_s: float | None = None,
) -> AgentServices:
    """Build the shared service layer from the selected adapters."""
    store = runtime.store
    if store is None:
        return AgentServices(None, None, None, None)
    return AgentServices(
        admin=AdminService(store),
        intake=IntakeService(runtime, store, turn_timeout_s=turn_timeout_s),
        orders=OrderActionService(
            OrderProcessingCore(store), store, storage, tally_push
        ),
        loading=LoadingService(store, notifier),
    )
