"""The agent-runtime seam and composition root (issue #54).

One runtime owns agent construction, session creation, and turn execution. The
web layer drives turns through the ``AgentRuntime`` interface and never imports
ADK or an adapter directly. ``build_runtime`` is the single construction path:
production selects adapters from configuration, and tests inject in-memory
doubles through the same function.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from .agent import build_agent, build_session_service
from .agent_exec import TurnExecutor
from .config import settings
from .media import MediaObject
from .meta_whatsapp import MetaWhatsAppSender
from .model_provider import ModelCapabilities, ModelProvider, build_model_provider
from .store import InMemoryOrderStore, LoopProxiedStore, OrderStore
from .voucher import VoucherStore, default_voucher_storage
from .whatsapp import WhatsAppSender

_UNSET: Any = object()


class AgentRuntime(Protocol):
    """Everything the web layer needs from the agent runtime."""

    @property
    def capabilities(self) -> ModelCapabilities: ...

    @property
    def store(self) -> OrderStore | None: ...

    @property
    def voucher_storage(self) -> VoucherStore | None: ...

    @property
    def whatsapp_sender(self) -> WhatsAppSender: ...

    def run_turn(
        self, *, sender_id: str, message: str, media: MediaObject | None = None
    ) -> str: ...

    def stop(self) -> None: ...


class AdkAgentRuntime:
    """ADK-backed runtime: builds the agent and session on one long-lived loop."""

    def __init__(
        self,
        *,
        agent_session_factory: Callable[[], tuple[object, object]],
        capabilities: ModelCapabilities,
        store: OrderStore | None,
        voucher_storage: VoucherStore | None,
        whatsapp_sender: WhatsAppSender,
        proxy_store: bool = False,
    ) -> None:
        self._capabilities = capabilities
        self._voucher_storage = voucher_storage
        self._whatsapp_sender = whatsapp_sender
        self._executor = TurnExecutor(agent_session_factory, capabilities=capabilities)
        self._store = (
            LoopProxiedStore(store, self._executor.loop)
            if proxy_store and store is not None
            else store
        )

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._capabilities

    @property
    def store(self) -> OrderStore | None:
        return self._store

    @property
    def voucher_storage(self) -> VoucherStore | None:
        return self._voucher_storage

    @property
    def whatsapp_sender(self) -> WhatsAppSender:
        return self._whatsapp_sender

    def run_turn(
        self, *, sender_id: str, message: str, media: MediaObject | None = None
    ) -> str:
        return self._executor.run_turn(
            sender_id=sender_id, message=message, media=media
        )

    def stop(self) -> None:
        self._executor.stop()


def _select_store() -> tuple[OrderStore | None, bool]:
    """Select the configured OrderStore and whether web calls need a loop proxy."""
    kind = settings.order_store or {
        "firestore": "firestore",
        "sqlite": "sqlite",
    }.get(settings.session_service, "memory")
    if kind == "memory":
        return InMemoryOrderStore(), False
    if kind == "sqlite":
        from .sql_store import SqlOrderStore

        return SqlOrderStore(settings.database_url), False
    if kind == "firestore":
        from .store import FirestoreOrderStore

        return FirestoreOrderStore(), True
    raise ValueError(
        f"Unknown ORDER_STORE {kind!r}; expected 'memory', 'sqlite' or 'firestore'"
    )


def build_runtime(
    *,
    store: OrderStore | None = _UNSET,
    voucher_storage: VoucherStore | None = _UNSET,
    model_provider: ModelProvider | None = None,
    whatsapp_sender: WhatsAppSender | None = None,
    agent: object | None = None,
    session_service: object | None = None,
) -> AdkAgentRuntime:
    """Compose one runtime, selecting any adapter not explicitly injected.

    Omitting a dependency selects the configured adapter; passing ``None``
    leaves it absent (the web layer then skips the routes that need it). Tests
    inject in-memory adapters here, so they and production share this path.
    """
    if store is _UNSET:
        selected_store, proxy_store = _select_store()
    else:
        selected_store, proxy_store = store, False
    selected_voucher = (
        default_voucher_storage(
            bucket=settings.voucher_bucket, directory=settings.voucher_dir
        )
        if voucher_storage is _UNSET
        else voucher_storage
    )
    selected_provider = (
        build_model_provider() if model_provider is None else model_provider
    )
    selected_sender = (
        MetaWhatsAppSender(
            settings.meta_access_token, settings.meta_phone_number_id
        )
        if whatsapp_sender is None
        else whatsapp_sender
    )

    def agent_session_factory() -> tuple[object, object]:
        if session_service is not None:
            session = session_service
        else:
            from .sql_store import SqlOrderStore

            shared_engine = (
                selected_store.engine
                if isinstance(selected_store, SqlOrderStore)
                else None
            )
            session = build_session_service(engine=shared_engine)
        built_agent = (
            agent
            if agent is not None
            else build_agent(
                provider=selected_provider,
                store=selected_store,
                whatsapp_sender=selected_sender,
                voucher_storage=selected_voucher,
            )
        )
        return built_agent, session

    return AdkAgentRuntime(
        agent_session_factory=agent_session_factory,
        capabilities=selected_provider.capabilities,
        store=selected_store,
        voucher_storage=selected_voucher,
        whatsapp_sender=selected_sender,
        proxy_store=proxy_store,
    )
