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
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Protocol

from .config import settings
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

# A worker that loses a cross-process claim waits this long between polls.
IDEMPOTENCY_POLL_INTERVAL_S = 0.05

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

    async def run_turn_async(
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


class ClaimStatus(Enum):
    """The outcome of trying to own an idempotency key."""

    CLAIMED = "claimed"
    PENDING = "pending"
    COMPLETE = "complete"


@dataclass(frozen=True)
class IdempotencyClaim:
    """A claim on ``(scope, key)``; a completed claim carries the stored reply."""

    status: ClaimStatus
    fingerprint: str = ""
    result: dict[str, Any] | None = None


@dataclass
class _IdempotencyEntry:
    fingerprint: str
    result: dict[str, Any] | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    expires_at: str = ""


class IdempotencyStore(Protocol):
    """Atomically claim, complete, and expire one response per scoped key."""

    async def try_claim(
        self, scope: str, key: str, fingerprint: str
    ) -> IdempotencyClaim: ...

    async def complete(
        self, scope: str, key: str, fingerprint: str, result: dict[str, Any]
    ) -> None: ...

    async def release(self, scope: str, key: str) -> None: ...

    async def cleanup_expired(self) -> int: ...


class InMemoryIdempotencyStore:
    """Single-process adapter for tests and fake mode, cleared on restart."""

    def __init__(self, *, ttl_s: float | None = None) -> None:
        self._ttl_s = settings.idempotency_ttl_s if ttl_s is None else ttl_s
        self._entries: dict[tuple[str, str], _IdempotencyEntry] = {}
        self._lock = asyncio.Lock()

    def _expiry(self) -> str:
        return (datetime.now(UTC) + timedelta(seconds=self._ttl_s)).isoformat()

    async def try_claim(
        self, scope: str, key: str, fingerprint: str
    ) -> IdempotencyClaim:
        async with self._lock:
            identity = (scope, key)
            now = datetime.now(UTC).isoformat()
            entry = self._entries.get(identity)
            if entry is not None and entry.expires_at <= now:
                del self._entries[identity]
                entry = None
            if entry is None:
                self._entries[identity] = _IdempotencyEntry(
                    fingerprint=fingerprint, expires_at=self._expiry()
                )
                return IdempotencyClaim(ClaimStatus.CLAIMED)
            if entry.result is not None:
                return IdempotencyClaim(
                    ClaimStatus.COMPLETE, entry.fingerprint, dict(entry.result)
                )
            return IdempotencyClaim(ClaimStatus.PENDING)

    async def complete(
        self, scope: str, key: str, fingerprint: str, result: dict[str, Any]
    ) -> None:
        async with self._lock:
            entry = self._entries.get((scope, key))
            if entry is not None and entry.fingerprint == fingerprint:
                entry.result = dict(result)

    async def release(self, scope: str, key: str) -> None:
        async with self._lock:
            identity = (scope, key)
            entry = self._entries.get(identity)
            if entry is not None and entry.result is None:
                del self._entries[identity]

    async def cleanup_expired(self) -> int:
        async with self._lock:
            now = datetime.now(UTC).isoformat()
            expired = [k for k, e in self._entries.items() if e.expires_at <= now]
            for identity in expired:
                del self._entries[identity]
            return len(expired)


class IdempotentExecutor:
    """Run one operation once per scoped key, then replay its stored result.

    One executor owns the key locks for a service, so concurrent callers in the
    same process queue on the key; the store's atomic claim serializes workers
    in different processes. Callers supply the operation and its fingerprint —
    this class only decides whether it runs, waits, or replays.
    """

    def __init__(self, store: IdempotencyStore) -> None:
        self._store = store
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def _lock_for(self, identity: str) -> asyncio.Lock:
        async with self._guard:
            return self._locks.setdefault(identity, asyncio.Lock())

    async def run(
        self,
        *,
        scope: str,
        key: str | None,
        fingerprint: str,
        execute: Callable[[], Awaitable[dict[str, Any]]],
    ) -> tuple[dict[str, Any], bool]:
        """Return the operation's result and whether it was a replay."""
        if not key:
            return await execute(), False
        lock = await self._lock_for(f"{scope}:{key}")
        async with lock:
            while True:
                claim = await self._store.try_claim(scope, key, fingerprint)
                if claim.status is ClaimStatus.COMPLETE:
                    if claim.fingerprint != fingerprint:
                        raise IdempotencyConflict(
                            "idempotency key was reused with a different payload"
                        )
                    return dict(claim.result or {}), True
                if claim.status is ClaimStatus.CLAIMED:
                    try:
                        result = await execute()
                    except BaseException:
                        await self._store.release(scope, key)
                        raise
                    await self._store.complete(scope, key, fingerprint, result)
                    return result, False
                await asyncio.sleep(IDEMPOTENCY_POLL_INTERVAL_S)


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
        turn_timeout_s: float | None = None,
    ) -> None:
        self._runner = runner
        self._store = store
        self._turn_timeout_s = (
            settings.agent_turn_timeout_s if turn_timeout_s is None else turn_timeout_s
        )
        self._capabilities: ModelCapabilities = runner.capabilities
        self._idempotent = IdempotentExecutor(
            idempotency or InMemoryIdempotencyStore()
        )
        self._sender_locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _sender_lock(self, sender_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._sender_locks.setdefault(sender_id, asyncio.Lock())

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
        async def execute() -> dict[str, Any]:
            result = await self._execute(sender_id, message, media)
            return result.to_dict()

        stored, replay = await self._idempotent.run(
            scope=f"{principal_id}:{surface}",
            key=idempotency_key,
            fingerprint=fingerprint,
            execute=execute,
        )
        return IntakeResult(**{**stored, "idempotent": replay})

    async def _execute(
        self, sender_id: str, message: str, media: MediaObject | None
    ) -> IntakeResult:
        # One sender's turns are serialized: the decision is derived from a
        # store diff, so two overlapping turns for one phone must not each
        # claim the order the other committed.
        async with await self._sender_lock(sender_id):
            before = {order.order_id for order in await self._store.list_all_orders()}
            started_at = utcnow()
            reply = await asyncio.wait_for(
                self._runner.run_turn_async(
                    sender_id=sender_id, message=message, media=media
                ),
                timeout=self._turn_timeout_s,
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


def valid_e164(value: str) -> bool:
    """True only for a whole-string E.164 number."""
    return re.fullmatch(E164_PATTERN, value) is not None


def validate_intake_sender(sender_id: str) -> str:
    """Validate the customer phone both REST and MCP accept for a text order."""
    if not valid_e164(sender_id):
        raise ValueError("sender_id must be an E.164 phone number")
    return sender_id


def validate_intake_message(message: str) -> str:
    """Validate the customer text both REST and MCP accept for a text order."""
    if not message.strip():
        raise ValueError("message is required")
    return message


def validate_idempotency_key(key: str | None) -> str | None:
    """Validate a caller-supplied idempotency key for both transports."""
    if key is None:
        return None
    key = key.strip()
    if not key or len(key) > 255:
        raise ValueError("invalid Idempotency-Key header")
    return key


def intake_fingerprint(
    payload: dict[str, Any], path: str = "/api/v1/intake"
) -> str:
    """Fingerprint an intake payload; callers pass their route path."""
    return fingerprint_of(path, payload)
