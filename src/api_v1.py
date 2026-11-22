"""REST v1 bootstrap and key routes; all auth flows through ``AuthService``.

This router is intentionally small: it owns first-run setup and key management
(issue #57). The remaining Agent Interface resources land in later tickets and
call the same :func:`build_api_v1_router` dependencies, so REST and MCP cannot
diverge on identity, roles, or confirmation.
"""

from __future__ import annotations

from collections.abc import Collection

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .auth import (
    ADMIN_ROLES,
    READ_ROLES,
    AuthError,
    AuthService,
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


def _fail(error: AuthError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=str(error))


def build_api_v1_router(auth: AuthService) -> APIRouter:
    """Build the authenticated ``/api/v1`` router for one AuthService."""

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

    async def admin(request: Request) -> Principal:
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
    async def instance(request: Request) -> dict[str, str]:
        principal = await identify(request)
        profile = await auth.get_instance()
        return {
            "business_name": profile.business_name if profile else "",
            "timezone": profile.timezone if profile else "UTC",
            "role": principal.role.value,
            "principal": principal.id,
        }

    @router.post("/keys", status_code=201)
    async def issue_key(payload: KeyRequest, request: Request) -> IssuedKey:
        await admin(request)
        try:
            return await auth.issue_key(
                name=payload.name, role=payload.role, phone=payload.phone
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.get("/keys")
    async def list_keys(request: Request) -> list[dict[str, object]]:
        await admin(request)
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
        await admin(request)
        try:
            await auth.revoke_key(key_id)
        except AuthError as error:
            raise _fail(error) from error

    return router
