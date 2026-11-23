"""REST v1 admin, bootstrap and key routes; all auth flows through ``AuthService``.

This router owns first-run setup, key management, catalogue/config
administration, and Integration health (issues #57 and #58). Rest dependencies
and the MCP gateway call the same :class:`AuthService`, so neither can grant a
principal more power than the other.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Collection
from datetime import date as _date
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

from .admin import AdminService, CatalogValidationError
from .auth import (
    ADMIN_ROLES,
    APPROVER_ROLES,
    INTAKE_ROLES,
    READ_ROLES,
    AuthError,
    AuthService,
    InstanceProfile,
    IssuedKey,
    Principal,
    Role,
    bearer_token,
)
from .core import ApprovalError
from .instance_service import instance_view
from .intake import (
    E164_PATTERN,
    CapabilityError,
    IdempotencyConflict,
    IntakeService,
    InvalidMedia,
    MediaTooLarge,
    decode_attachment,
    intake_fingerprint,
    validate_idempotency_key,
    validate_intake_message,
    validate_intake_sender,
)
from .loading_service import LoadingService, parse_delivery_day
from .order_actions import (
    OrderActionService,
    order_detail,
    order_summary,
    select_orders,
)
from .store import OrderStore
from .tally import TallyPushError, TallyPushFailed
from .voucher import VoucherError
from .whatsapp import WhatsAppSender

# An outbound send is a phone number plus a short text. A small fixed cap keeps
# a privileged caller from buffering an arbitrary body in the web worker.
MAX_ADMIN_SEND_MESSAGE_BODY_BYTES = 64 * 1024
MAX_TEXT_INTAKE_BODY_BYTES = 64 * 1024
MAX_MEDIA_INTAKE_BODY_BYTES = 8 * 1024 * 1024


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

    @field_validator("sender_id")
    @classmethod
    def _whole_e164(cls, value: str) -> str:
        return validate_intake_sender(value)

    @field_validator("message")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        return validate_intake_message(value)


class VoiceIntakeRequest(BaseModel):
    caller: str = Field(pattern=E164_PATTERN, description="Caller phone (E.164)")
    audio_base64: str = Field(min_length=1)
    mime_type: str = "audio/wav"


class PhotoIntakeRequest(BaseModel):
    sender_id: str = Field(pattern=E164_PATTERN, description="Sender phone (E.164)")
    image_base64: str = Field(min_length=1)
    mime_type: str = "image/jpeg"
    message: str = ""


class AdminSendMessageRequest(BaseModel):
    to: str = Field(pattern=E164_PATTERN, description="Recipient phone (E.164)")
    text: str = Field(min_length=1, description="Message body")

    @field_validator("to")
    @classmethod
    def _whole_e164(cls, value: str) -> str:
        return validate_intake_sender(value)


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


class OrderEditRequest(BaseModel):
    customer: str | None = None
    delivery_location: str | None = None
    items: list[dict[str, Any]] | None = None
    gst_override_pct: float | None = None
    customer_id: str | None = None
    product_resolutions: list[dict[str, Any]] | None = None


class OrderActionRequest(BaseModel):
    action: Literal[
        "approve",
        "reject",
        "dispatch",
        "prepare_voucher",
        "push_to_tally",
        "bill",
    ]
    confirm: bool = False


class OrderActionResponse(BaseModel):
    order_id: str
    action: str
    status: str
    order: OrderDetailResponse


class LoadingListResponse(BaseModel):
    delivery_day: str
    cutoff_time: str
    sections: list[dict[str, Any]]
    unrouted: list[dict[str, Any]]
    late: list[dict[str, Any]]


class StatsResponse(BaseModel):
    pending_escalations: int
    approved_today: int
    billed_today: int
    late_orders: int


def _fail(error: AuthError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=str(error))


def _invalid(error: ValueError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(error))


def _unprocessable(error: Exception) -> HTTPException:
    return HTTPException(status_code=422, detail=str(error))


async def _read_bounded_json[T: BaseModel](
    request: Request, *, cap: int, model: type[T]
) -> T:
    """Stream a request body under ``cap`` bytes, then validate it as ``model``.

    FastAPI's implicit Pydantic body parsing buffers the whole request before a
    field validator can reject it, so security-relevant routes read the stream
    explicitly and bound it first (issues #75 and #90).
    """
    size = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        size += len(chunk)
        if size > cap:
            raise HTTPException(status_code=413, detail="request body too large")
        chunks.append(chunk)
    try:
        payload = json.loads(b"".join(chunks))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="invalid JSON") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    try:
        return model.model_validate(payload)
    except ValueError as error:
        raise _invalid(error) from error


def _delivery_day(value: str | None) -> _date | None:
    try:
        return parse_delivery_day(value)
    except ValueError as error:
        raise _invalid(error) from error


def build_api_v1_router(
    auth: AuthService,
    admin_service: AdminService | None = None,
    intake_service: IntakeService | None = None,
    store: OrderStore | None = None,
    order_service: OrderActionService | None = None,
    loading_service: LoadingService | None = None,
    whatsapp_sender: WhatsAppSender | None = None,
) -> APIRouter:
    """Build the authenticated ``/api/v1`` router for one Instance."""

    router = APIRouter(prefix="/api/v1", tags=["agent-interface"])

    async def identify(
        request: Request,
        *,
        roles: Collection[Role] = READ_ROLES,
        action: str | None = None,
        confirm: bool = False,
    ) -> Principal:
        try:
            return await auth.require(
                bearer_token(request.headers.get("Authorization")),
                roles=roles,
                action=action,
                confirm=confirm,
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
        return await instance_view(auth, admin_service, principal)

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
        try:
            key = validate_idempotency_key(request.headers.get("idempotency-key"))
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return key, intake_fingerprint(payload.model_dump(), request.url.path)

    if intake_service is not None:

        @router.post(
            "/intake",
            response_model=IntakeResponse,
            tags=["intake"],
            openapi_extra={
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": IntakeRequest.model_json_schema()
                        }
                    },
                }
            },
        )
        async def intake_order(request: Request) -> IntakeResponse:
            principal = await identify(request, roles=INTAKE_ROLES)
            payload = await _read_bounded_json(
                request, cap=MAX_TEXT_INTAKE_BODY_BYTES, model=IntakeRequest
            )
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
            except TimeoutError as error:
                raise HTTPException(
                    status_code=504, detail="agent turn timed out"
                ) from error
            return IntakeResponse(**result.to_dict())

        async def _media_intake(
            *,
            request: Request,
            model: type[BaseModel],
            audio: bool,
        ) -> IntakeResponse:
            principal = await identify(request, roles=INTAKE_ROLES)
            payload = await _read_bounded_json(
                request,
                cap=MAX_MEDIA_INTAKE_BODY_BYTES,
                model=model,
            )
            key, fingerprint = _idempotency(request, payload)
            fields = payload.model_dump()
            sender_id = str(fields["caller" if audio else "sender_id"])
            data = str(fields["audio_base64" if audio else "image_base64"])
            mime_type = str(fields["mime_type"])
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
                        message=str(fields.get("message", "")),
                        principal_id=principal.id,
                        idempotency_key=key,
                        fingerprint=fingerprint,
                    )
            except CapabilityError as error:
                raise HTTPException(status_code=503, detail=str(error)) from error
            except IdempotencyConflict as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            except TimeoutError as error:
                raise HTTPException(
                    status_code=504, detail="agent turn timed out"
                ) from error
            return IntakeResponse(**result.to_dict())

        @router.post(
            "/intake/voice",
            response_model=IntakeResponse,
            tags=["intake"],
            openapi_extra={
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": VoiceIntakeRequest.model_json_schema()
                        }
                    },
                }
            },
        )
        async def intake_voice(request: Request) -> IntakeResponse:
            return await _media_intake(
                request=request,
                model=VoiceIntakeRequest,
                audio=True,
            )

        @router.post(
            "/intake/photo",
            response_model=IntakeResponse,
            tags=["intake"],
            openapi_extra={
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": PhotoIntakeRequest.model_json_schema()
                        }
                    },
                }
            },
        )
        async def intake_photo(request: Request) -> IntakeResponse:
            return await _media_intake(
                request=request,
                model=PhotoIntakeRequest,
                audio=False,
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
            try:
                orders = select_orders(
                    await store.list_all_orders(),
                    status=status,
                    customer=customer,
                    phone=phone,
                    date=date,
                )
            except ValueError as error:
                raise _invalid(error) from error
            return [OrderSummaryResponse(**order_summary(order)) for order in orders]

        @router.get(
            "/orders/{order_id}", response_model=OrderDetailResponse, tags=["orders"]
        )
        async def get_order(order_id: str, request: Request) -> OrderDetailResponse:
            await identify(request)
            order = await store.get_order(order_id)
            if order is None:
                raise HTTPException(status_code=404, detail="order not found")
            events = await store.list_order_events(order_id)
            return OrderDetailResponse(**order_detail(order, events))

        if order_service is not None:

            @router.patch(
                "/orders/{order_id}",
                response_model=OrderDetailResponse,
                tags=["orders"],
            )
            async def edit_order(
                order_id: str, payload: OrderEditRequest, request: Request
            ) -> OrderDetailResponse:
                principal = await identify(request, roles=APPROVER_ROLES)
                try:
                    result = await order_service.edit(
                        {
                            "order_id": order_id,
                            "changes": payload.model_dump(exclude_unset=True),
                        },
                        principal,
                    )
                except (ApprovalError, ValueError) as error:
                    raise _unprocessable(error) from error
                return OrderDetailResponse(**result)

            @router.post(
                "/orders/{order_id}/actions",
                response_model=OrderActionResponse,
                tags=["orders"],
            )
            async def act_on_order(
                order_id: str, payload: OrderActionRequest, request: Request
            ) -> OrderActionResponse:
                principal = await identify(
                    request,
                    roles=APPROVER_ROLES,
                    action=payload.action,
                    confirm=payload.confirm,
                )
                try:
                    result = await order_service.act(
                        {"order_id": order_id, "action": payload.action}, principal
                    )
                except TallyPushFailed as error:
                    raise HTTPException(
                        status_code=502, detail=str(error)
                    ) from error
                except (
                    ApprovalError,
                    ValueError,
                    VoucherError,
                    TallyPushError,
                ) as error:
                    raise _unprocessable(error) from error
                return OrderActionResponse(**result)

            @router.get(
                "/orders/{order_id}/voucher",
                response_class=Response,
                tags=["orders"],
            )
            async def download_voucher(order_id: str, request: Request) -> Response:
                principal = await identify(request, roles=APPROVER_ROLES)
                order = await store.get_order(order_id)
                if order is None:
                    raise HTTPException(status_code=404, detail="order not found")
                try:
                    xml = await order_service.voucher_xml(
                        {"order_id": order_id}, principal
                    )
                except VoucherError as error:
                    raise _unprocessable(error) from error
                return Response(content=xml, media_type="application/xml")

    if loading_service is not None:

        @router.get(
            "/loading-lists",
            response_model=LoadingListResponse,
            tags=["loading"],
        )
        async def get_loading_list(
            request: Request, day: str | None = None
        ) -> LoadingListResponse:
            await identify(request)
            return LoadingListResponse(
                **await loading_service.render_loading_list(_delivery_day(day))
            )

        @router.get("/stats", response_model=StatsResponse, tags=["stats"])
        async def get_stats(request: Request) -> StatsResponse:
            await identify(request)
            return StatsResponse(**await loading_service.stats())

        @router.post(
            "/cutoff/render",
            response_model=LoadingListResponse,
            tags=["loading"],
        )
        async def render_cutoff(
            request: Request, day: str | None = None
        ) -> LoadingListResponse:
            await require_admin(request)
            return LoadingListResponse(
                **await loading_service.render_cutoff(_delivery_day(day))
            )

    @router.post("/admin/send-message", tags=["admin"])
    async def admin_send_message(request: Request) -> dict[str, bool]:
        """Send one outbound WhatsApp message as the authenticated Business."""
        await require_admin(request)
        if whatsapp_sender is None:
            raise HTTPException(status_code=503, detail="messaging is not configured")
        message = await _read_bounded_json(
            request,
            cap=MAX_ADMIN_SEND_MESSAGE_BODY_BYTES,
            model=AdminSendMessageRequest,
        )
        await asyncio.to_thread(whatsapp_sender.send, message.to, message.text)
        return {"sent": True}

    return router
