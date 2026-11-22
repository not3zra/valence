"""REST v1 admin, bootstrap and key routes; all auth flows through ``AuthService``.

This router owns first-run setup, key management, catalogue/config
administration, and Integration health (issues #57 and #58). Rest dependencies
and the MCP gateway call the same :class:`AuthService`, so neither can grant a
principal more power than the other.
"""

from __future__ import annotations

from collections.abc import Collection
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


def _fail(error: AuthError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=str(error))


def _invalid(error: ValueError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(error))


def build_api_v1_router(
    auth: AuthService, admin_service: AdminService | None = None
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

    return router
