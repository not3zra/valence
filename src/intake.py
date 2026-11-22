"""REST intake and order-inspection service layer (issue #59).

One :class:`IntakeService` runs an agent turn and turns whatever the agent
committed into a decision the Agent Interface can return. It is deliberately
narrow: a turn runner, an order store, and an idempotency store. It imports no
ADK types, so it can be exercised entirely through the HTTP seam with fakes.

Identity rule (issue #50, "Authentication and safety"): authorization comes
from the authenticated key's principal, never from a phone in the request
body. ``sender_id`` is not an identity for authorization — it is the customer
whose order an authorized operator is placing, the session/order identity the
agent uses to resolve a verified catalog customer by phone. That is the same
trusted-metadata contract as the existing company-recorded voice ingest path;
the API key, not the phone field, decides what the caller may do.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from .media import AUDIO_MIME_TYPES, MAX_MEDIA_BYTES, MediaObject
from .model_provider import ModelCapabilities
from .orders import (
    EVENT_ORDER_DUPLICATE,
    Order,
    OrderStatus,
    iso_to_dt,
    utcnow,
)
from .store import OrderStore

E164_PATTERN = r"^\+[1-9]\d{1,14}$"

IMAGE_MIME_TYPES: frozenset[str] = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/gif"}
)

# The voice path has no customer text to read; this nudge tells the agent the
# inline audio is a call in which an order was placed (same as /api/voice/ingest).
VOICE_NUDGE = (
    "This audio is a recording of a phone call in which the caller placed an "
    "order. Understand the recording and commit it as a structured order."
)


class CapabilityError(Exception):
    """The configured model cannot understand the requested media type."""


class IdempotencyConflict(Exception):
    """An idempotency key was reused with a different payload."""


class InvalidMedia(ValueError):
    """The uploaded media is malformed or of an unsupported type."""


class MediaTooLarge(ValueError):
    """The uploaded media exceeds the configured byte cap."""


class TurnRunner(Protocol):
    """The slice of AgentRuntime the intake service needs."""

    @property
    def capabilities(self) -> ModelCapabilities: ...

    def run_turn(
        self, *, sender_id: str, message: str, media: MediaObject | None = None
    ) -> str: ...


@dataclass(frozen=True)
class IntakeResult:
    """The agent reply plus the decision that was persisted for it."""

    reply: str
    order_id: str | None
    decision: dict[str, Any]
    duplicate: bool = False
    idempotent: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class IdempotencyStore(Protocol):
    """Cache one response per key, together with a request fingerprint."""

    async def get(self, key: str) -> tuple[str, dict[str, Any]] | None: ...

    async def set(
        self, key: str, fingerprint: str, result: dict[str, Any]
    ) -> None: ...


class InMemoryIdempotencyStore:
    """Single-instance adapter; exact for one process, cleared on restart."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[str, dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> tuple[str, dict[str, Any]] | None:
        async with self._lock:
            return self._entries.get(key)

    async def set(
        self, key: str, fingerprint: str, result: dict[str, Any]
    ) -> None:
        async with self._lock:
            self._entries[key] = (fingerprint, result)


def decode_attachment(data: str, mime_type: str, *, audio: bool) -> MediaObject:
    """Decode and validate a base64 attachment into a provider-neutral object."""
    # Bound the encoded size before decoding: a multi-gigabyte base64 body must
    # not be expanded into memory just to be rejected afterwards (CWE-400).
    encoded_limit = 4 * ((MAX_MEDIA_BYTES + 2) // 3)
    if len(data) > encoded_limit:
        raise MediaTooLarge("media exceeds the size limit")
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, TypeError) as error:
        raise InvalidMedia("invalid base64 media payload") from error
    if not raw:
        raise InvalidMedia("empty media payload")
    if len(raw) > MAX_MEDIA_BYTES:
        raise MediaTooLarge("media exceeds the size limit")
    mime = mime_type.strip().lower()
    allowed = AUDIO_MIME_TYPES if audio else IMAGE_MIME_TYPES
    if mime not in allowed:
        kind = "audio" if audio else "image"
        raise InvalidMedia(f"unsupported {kind} mime type")
    return MediaObject(data=raw, mime_type=mime)


def _decision(**values: Any) -> dict[str, Any]:
    """One decision shape for every turn outcome, committed or held."""
    return {
        "order_id": None,
        "status": OrderStatus.PENDING_REVIEW.value,
        "approved": False,
        "escalation_reasons": [],
        "draft_value_inr": 0.0,
        "customer_id": None,
        "delivery_location_id": None,
        "items": [],
        "source_channel": "",
        "duplicate": False,
        "clarify": False,
        "missing_fields": [],
        "late": False,
        "voucher_id": None,
        **values,
    }


def _decision_from_order(order: Order) -> dict[str, Any]:
    """The authoritative decision is the order the agent actually persisted."""
    return _decision(
        order_id=order.order_id,
        status=order.status.value,
        approved=order.status
        in (OrderStatus.APPROVED, OrderStatus.DISPATCHED, OrderStatus.BILLED),
        escalation_reasons=list(order.escalation_reasons),
        draft_value_inr=order.draft_value_inr,
        customer_id=order.customer_id,
        delivery_location_id=order.delivery_location_id,
        items=[asdict(item) for item in order.items],
        source_channel=order.source_channel,
        voucher_id=order.voucher_id,
    )


class IntakeService:
    """Run one turn, surface its committed decision, and make retries safe."""

    def __init__(
        self,
        runner: TurnRunner,
        store: OrderStore,
        *,
        idempotency: IdempotencyStore | None = None,
    ) -> None:
        self._runner = runner
        self._store = store
        self._capabilities: ModelCapabilities = runner.capabilities
        self._idempotency = idempotency or InMemoryIdempotencyStore()
        self._locks: dict[str, asyncio.Lock] = {}
        self._sender_locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _lock_for(
        self, registry: dict[str, asyncio.Lock], key: str
    ) -> asyncio.Lock:
        async with self._locks_guard:
            return registry.setdefault(key, asyncio.Lock())

    async def intake_text(
        self,
        *,
        sender_id: str,
        message: str,
        principal_id: str,
        idempotency_key: str | None = None,
        fingerprint: str = "",
    ) -> IntakeResult:
        return await self._run(
            surface="text",
            sender_id=sender_id,
            message=message,
            media=None,
            principal_id=principal_id,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint,
        )

    async def intake_voice(
        self,
        *,
        caller: str,
        media: MediaObject,
        principal_id: str,
        idempotency_key: str | None = None,
        fingerprint: str = "",
    ) -> IntakeResult:
        if not self._capabilities.audio:
            raise CapabilityError("voice intake is not supported by the model provider")
        return await self._run(
            surface="voice",
            sender_id=caller,
            message=VOICE_NUDGE,
            media=media,
            principal_id=principal_id,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint,
        )

    async def intake_photo(
        self,
        *,
        sender_id: str,
        media: MediaObject,
        message: str = "",
        principal_id: str,
        idempotency_key: str | None = None,
        fingerprint: str = "",
    ) -> IntakeResult:
        if not self._capabilities.image:
            raise CapabilityError("photo intake is not supported by the model provider")
        return await self._run(
            surface="photo",
            sender_id=sender_id,
            message=message,
            media=media,
            principal_id=principal_id,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint,
        )

    async def _run(
        self,
        *,
        surface: str,
        sender_id: str,
        message: str,
        media: MediaObject | None,
        principal_id: str,
        idempotency_key: str | None,
        fingerprint: str,
    ) -> IntakeResult:
        if not idempotency_key:
            return await self._execute(sender_id, message, media)
        key = f"{principal_id}:{surface}:{idempotency_key}"
        lock = await self._lock_for(self._locks, key)
        async with lock:
            cached = await self._idempotency.get(key)
            if cached is not None:
                stored_fingerprint, stored_result = cached
                if stored_fingerprint != fingerprint:
                    raise IdempotencyConflict(
                        "idempotency key was reused with a different payload"
                    )
                return IntakeResult(**{**stored_result, "idempotent": True})
            result = await self._execute(sender_id, message, media)
            await self._idempotency.set(key, fingerprint, result.to_dict())
            return result

    async def _execute(
        self, sender_id: str, message: str, media: MediaObject | None
    ) -> IntakeResult:
        # One sender's turns are serialized: the decision is derived from a
        # store diff, so two overlapping turns for one phone must not each
        # claim the order the other committed.
        async with await self._lock_for(self._sender_locks, sender_id):
            before = {order.order_id for order in await self._store.list_all_orders()}
            started_at = utcnow()
            reply = await asyncio.to_thread(
                self._runner.run_turn,
                sender_id=sender_id,
                message=message,
                media=media,
            )
            decision = await self._decision(sender_id, before, started_at)
            return IntakeResult(
                reply=reply,
                order_id=decision.get("order_id"),
                decision=decision,
                duplicate=bool(decision.get("duplicate")),
            )

    async def _decision(
        self, sender_id: str, before: set[str | None], started_at: str
    ) -> dict[str, Any]:
        orders = await self._store.list_all_orders()
        created = [
            order
            for order in orders
            if order.order_id not in before and order.phone == sender_id
        ]
        if created:
            newest = max(created, key=lambda order: order.created_at)
            return _decision_from_order(newest)

        start = iso_to_dt(started_at)
        for order in sorted(
            (item for item in orders if item.phone == sender_id),
            key=lambda item: item.created_at,
            reverse=True,
        ):
            events = await self._store.list_order_events(order.order_id or "")
            duplicated = any(
                event.event_type == EVENT_ORDER_DUPLICATE
                and iso_to_dt(event.created_at) >= start
                for event in events
            )
            if duplicated:
                return _decision(
                    order_id=order.order_id,
                    status=order.status.value,
                    source_channel=order.source_channel,
                    duplicate=True,
                    voucher_id=order.voucher_id,
                )
        return _decision(clarify=True)


def fingerprint_of(*parts: object) -> str:
    """Stable hash of a request payload, used to detect key reuse."""
    encoded = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
