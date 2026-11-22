"""REST v1 admin, bootstrap and key routes; all auth flows through ``AuthService``.

This router owns first-run setup, key management, catalogue/config
administration, and Integration health (issues #57 and #58). Rest dependencies
and the MCP gateway call the same :class:`AuthService`, so neither can grant a
principal more power than the other.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import asdict
from datetime import date as _date
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .admin import AdminService, CatalogValidationError
from .auth import (
    ADMIN_ROLES,
    READ_ROLES,
    AuthError,
    AuthService,
    InstanceProfile,
    IssuedKey,
    Principal,
    Role,
    bearer_token,
)
from .intake import (
    E164_PATTERN,
    CapabilityError,
    IdempotencyConflict,
    IntakeService,
    InvalidMedia,
    MediaTooLarge,
    decode_attachment,
    fingerprint_of,
)
from .orders import Order, OrderEvent, OrderStatus
from .store import OrderStore


class SetupRequest(BaseModel):
    setup_token: str
    business_name: str = Field(min_length=1)
    timezone: str = "UTC"
    admin_name: str = "first-admin"


class KeyRequest(BaseModel):
    name: str = Field(min_length=1)
    role: Role
    phone: str | None = None


class InstanceUpdateRequest(BaseModel):
    business_name: str | None = None
    timezone: str | None = None
    config: dict[str, Any] | None = None


class IntakeRequest(BaseModel):
    sender_id: str = Field(
        pattern=E164_PATTERN,
        description="Verified customer phone (E.164) used as the session identity",
    )
    message: str = Field(min_length=1, description="Natural-language order text")


class VoiceIntakeRequest(BaseModel):
    caller: str = Field(pattern=E164_PATTERN, description="Caller phone (E.164)")
    audio_base64: str = Field(min_length=1)
    mime_type: str = "audio/wav"


class PhotoIntakeRequest(BaseModel):
    sender_id: str = Field(pattern=E164_PATTERN, description="Sender phone (E.164)")
    image_base64: str = Field(min_length=1)
    mime_type: str = "image/jpeg"
    message: str = ""


class IntakeResponse(BaseModel):
    reply: str
    order_id: str | None
    decision: dict[str, Any]
    duplicate: bool = False
    idempotent: bool = False


class OrderEventResponse(BaseModel):
    event_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: str


class OrderSummaryResponse(BaseModel):
    order_id: str | None
    status: str
    customer: str | None
    phone: str
    source_channel: str
    draft_value_inr: float
    escalation_reasons: list[str]
    voucher_id: str | None
    created_at: str


class OrderDetailResponse(OrderSummaryResponse):
    customer_id: str | None
    delivery_location: str | None
    delivery_location_id: str | None
    items: list[dict[str, Any]]
    confidence: float
    updated_at: str
    voucher: dict[str, Any]
    events: list[OrderEventResponse]


INTAKE_ROLES = (Role.ADMIN, Role.OPERATOR)


def _fail(error: AuthError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=str(error))


def _invalid(error: ValueError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(error))


def _order_summary(order: Order) -> dict[str, Any]:
    return {
        "order_id": order.order_id,
        "status": order.status.value,
        "customer": order.customer,
        "phone": order.phone,
        "source_channel": order.source_channel,
        "draft_value_inr": order.draft_value_inr,
        "escalation_reasons": list(order.escalation_reasons),
        "voucher_id": order.voucher_id,
        "created_at": order.created_at,
    }


def _order_detail(order: Order, events: list[OrderEvent]) -> dict[str, Any]:
    return {
        **_order_summary(order),
        "customer_id": order.customer_id,
        "delivery_location": order.delivery_location,
        "delivery_location_id": order.delivery_location_id,
        "items": [asdict(item) for item in order.items],
        "confidence": order.confidence,
        "updated_at": order.updated_at,
        "voucher": {
            "prepared": order.voucher_id is not None,
            "voucher_id": order.voucher_id,
        },
        "events": [
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "payload": dict(event.payload),
                "created_at": event.created_at,
            }
            for event in sorted(events, key=lambda event: event.created_at)
        ],
    }


def build_api_v1_router(
    auth: AuthService,
    admin_service: AdminService | None = None,
    intake_service: IntakeService | None = None,
    store: OrderStore | None = None,
) -> APIRouter:
    """Build the authenticated ``/api/v1`` router for one Instance."""

    router = APIRouter(prefix="/api/v1", tags=["agent-interface"])

    async def identify(
        request: Request, *, roles: Collection[Role] = READ_ROLES
    ) -> Principal:
        try:
            return await auth.require(
                bearer_token(request.headers.get("Authorization")), roles=roles
            )
        except AuthError as error:
            raise _fail(error) from error

    async def require_admin(request: Request) -> Principal:
        return await identify(request, roles=ADMIN_ROLES)

    @router.post("/setup", status_code=201)
    async def setup(payload: SetupRequest) -> IssuedKey:
        try:
            return await auth.bootstrap(
                setup_token=payload.setup_token,
                business_name=payload.business_name,
                timezone=payload.timezone,
                admin_name=payload.admin_name,
            )
        except AuthError as error:
            raise _fail(error) from error

    @router.get("/instance")
    async def instance(request: Request) -> dict[str, object]:
        principal = await identify(request)
        profile = await auth.get_instance()
        return {
            "business_name": profile.business_name if profile else "",
            "timezone": profile.timezone if profile else "UTC",
            "role": principal.role.value,
            "principal": principal.id,
            "config": await admin_service.get_config() if admin_service else {},
            "integrations": await auth.integration_health(),
        }

    @router.patch("/instance")
    async def update_instance(
        payload: InstanceUpdateRequest, request: Request
    ) -> dict[str, object]:
        await require_admin(request)
        current = await auth.get_instance()
        profile: InstanceProfile | None = current
        try:
            if payload.business_name is not None or payload.timezone is not None:
                profile = await auth.update_instance(
                    business_name=payload.business_name
                    if payload.business_name is not None
                    else (current.business_name if current else ""),
                    timezone=payload.timezone
                    if payload.timezone is not None
                    else (current.timezone if current else "UTC"),
                )
            else:
                profile = current
            config = (
                await admin_service.update_config(payload.config)
                if admin_service and payload.config is not None
                else (await admin_service.get_config() if admin_service else {})
            )
        except ValueError as error:
            raise _invalid(error) from error
        return {
            "business_name": profile.business_name if profile else "",
            "timezone": profile.timezone if profile else "UTC",
            "config": config,
            "integrations": await auth.integration_health(),
        }

    @router.get("/integrations")
    async def integration_health(request: Request) -> dict[str, dict[str, bool]]:
        await require_admin(request)
        return await auth.integration_health()

    @router.put("/integrations/{service}")
    async def set_integration(
        service: str, values: dict[str, str], request: Request
    ) -> dict[str, object]:
        await require_admin(request)
        try:
            await auth.set_integration(service, values)
        except ValueError as error:
            raise _invalid(error) from error
        return {"service": service, "configured": True}

    if admin_service is not None:

        @router.get("/catalog/{kind}")
        async def list_catalog(kind: str, request: Request) -> list[dict[str, Any]]:
            await identify(request)
            try:
                return await admin_service.list_catalog(kind)
            except CatalogValidationError as error:
                raise _invalid(error) from error

        @router.put("/catalog/{kind}")
        async def upsert_catalog(
            kind: str, items: list[dict[str, Any]], request: Request
        ) -> dict[str, object]:
            await require_admin(request)
            try:
                upserted = await admin_service.upsert_catalog(kind, items)
            except CatalogValidationError as error:
                raise _invalid(error) from error
            return {"kind": kind, "upserted": upserted}

    @router.post("/keys", status_code=201)
    async def issue_key(payload: KeyRequest, request: Request) -> IssuedKey:
        await require_admin(request)
        try:
            return await auth.issue_key(
                name=payload.name, role=payload.role, phone=payload.phone
            )
        except ValueError as error:
            raise _invalid(error) from error

    @router.get("/keys")
    async def list_keys(request: Request) -> list[dict[str, object]]:
        await require_admin(request)
        return [
            {
                "id": key.id,
                "name": key.name,
                "role": key.role.value,
                "phone": key.phone,
                "prefix": key.prefix,
                "created_at": key.created_at,
                "revoked_at": key.revoked_at,
            }
            for key in await auth.list_keys()
        ]

    @router.delete("/keys/{key_id}", status_code=204)
    async def revoke_key(key_id: str, request: Request) -> None:
        await require_admin(request)
        try:
            await auth.revoke_key(key_id)
        except AuthError as error:
            raise _fail(error) from error

    def _idempotency(request: Request, payload: BaseModel) -> tuple[str | None, str]:
        key = request.headers.get("idempotency-key")
        if key is not None:
            key = key.strip()
            if not key or len(key) > 255:
                raise HTTPException(
                    status_code=422, detail="invalid Idempotency-Key header"
                )
        return key, fingerprint_of(request.url.path, payload.model_dump())

    if intake_service is not None:

        @router.post("/intake", response_model=IntakeResponse, tags=["intake"])
        async def intake_order(
            payload: IntakeRequest, request: Request
        ) -> IntakeResponse:
            principal = await identify(request, roles=INTAKE_ROLES)
            key, fingerprint = _idempotency(request, payload)
            try:
                result = await intake_service.intake_text(
                    sender_id=payload.sender_id,
                    message=payload.message,
                    principal_id=principal.id,
                    idempotency_key=key,
                    fingerprint=fingerprint,
                )
            except IdempotencyConflict as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            return IntakeResponse(**result.to_dict())

        async def _media_intake(
            *,
            request: Request,
            payload: BaseModel,
            sender_id: str,
            data: str,
            mime_type: str,
            audio: bool,
            message: str = "",
        ) -> IntakeResponse:
            principal = await identify(request, roles=INTAKE_ROLES)
            key, fingerprint = _idempotency(request, payload)
            try:
                media = decode_attachment(data, mime_type, audio=audio)
            except MediaTooLarge as error:
                raise HTTPException(status_code=413, detail=str(error)) from error
            except InvalidMedia as error:
                raise _invalid(error) from error
            try:
                if audio:
                    result = await intake_service.intake_voice(
                        caller=sender_id,
                        media=media,
                        principal_id=principal.id,
                        idempotency_key=key,
                        fingerprint=fingerprint,
                    )
                else:
                    result = await intake_service.intake_photo(
                        sender_id=sender_id,
                        media=media,
                        message=message,
                        principal_id=principal.id,
                        idempotency_key=key,
                        fingerprint=fingerprint,
                    )
            except CapabilityError as error:
                raise HTTPException(status_code=503, detail=str(error)) from error
            except IdempotencyConflict as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            return IntakeResponse(**result.to_dict())

        @router.post("/intake/voice", response_model=IntakeResponse, tags=["intake"])
        async def intake_voice(
            payload: VoiceIntakeRequest, request: Request
        ) -> IntakeResponse:
            return await _media_intake(
                request=request,
                payload=payload,
                sender_id=payload.caller,
                data=payload.audio_base64,
                mime_type=payload.mime_type,
                audio=True,
            )

        @router.post("/intake/photo", response_model=IntakeResponse, tags=["intake"])
        async def intake_photo(
            payload: PhotoIntakeRequest, request: Request
        ) -> IntakeResponse:
            return await _media_intake(
                request=request,
                payload=payload,
                sender_id=payload.sender_id,
                data=payload.image_base64,
                mime_type=payload.mime_type,
                audio=False,
                message=payload.message,
            )

    if store is not None:

        @router.get(
            "/orders", response_model=list[OrderSummaryResponse], tags=["orders"]
        )
        async def list_orders(
            request: Request,
            status: str | None = None,
            customer: str | None = None,
            phone: str | None = None,
            date: str | None = None,
        ) -> list[OrderSummaryResponse]:
            await identify(request)
            wanted_status: OrderStatus | None = None
            if status is not None:
                try:
                    wanted_status = OrderStatus(status)
                except ValueError as error:
                    raise _invalid(error) from error
            wanted_day = None
            if date is not None:
                try:
                    wanted_day = _date.fromisoformat(date)
                except ValueError as error:
                    raise _invalid(error) from error
            orders = await store.list_all_orders()
            if wanted_status is not None:
                orders = [order for order in orders if order.status is wanted_status]
            if phone is not None:
                orders = [order for order in orders if order.phone == phone]
            if customer is not None:
                needle = customer.casefold()
                orders = [
                    order
                    for order in orders
                    if needle in (order.customer or "").casefold()
                ]
            if wanted_day is not None:
                day = wanted_day.isoformat()
                orders = [order for order in orders if order.created_at[:10] == day]
            orders.sort(key=lambda order: order.created_at, reverse=True)
            return [OrderSummaryResponse(**_order_summary(order)) for order in orders]

        @router.get(
            "/orders/{order_id}", response_model=OrderDetailResponse, tags=["orders"]
        )
        async def get_order(order_id: str, request: Request) -> OrderDetailResponse:
            await identify(request)
            order = await store.get_order(order_id)
            if order is None:
                raise HTTPException(status_code=404, detail="order not found")
            events = await store.list_order_events(order_id)
            return OrderDetailResponse(**_order_detail(order, events))

    return router
