"""Server-side API key identity and authorization (issue #57, ADR-0007/0009).

The Agent Interface has one authorization layer: :class:`AuthService`. REST
dependencies and the MCP gateway both call it, so neither can grant a principal
more power than the other. Keys are high-entropy bearer tokens; only a salted
PBKDF2 digest and a short lookup prefix are stored, and the plaintext is returned
exactly once at issuance. Approver keys are bound to an allowlisted phone.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
from collections.abc import Awaitable, Callable, Collection, Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol

TOKEN_PREFIX = "vk_"
LOOKUP_PREFIX_LENGTH = 8
PBKDF2_ITERATIONS = 120_000
INTEGRATION_SERVICES = frozenset({"meta", "gemini", "tally"})
INTEGRATION_FIELDS: dict[str, tuple[str, ...]] = {
    "meta": ("access_token", "app_secret", "phone_number_id", "verify_token"),
    "gemini": ("api_key",),
    "tally": ("url",),
}
REQUIRED_INTEGRATION_FIELDS: dict[str, tuple[str, ...]] = {
    "meta": ("access_token",),
    "gemini": ("api_key",),
    "tally": ("url",),
}
CONFIRMED_ACTIONS = frozenset(
    {
        "approve",
        "reject",
        "dispatch",
        "prepare_voucher",
        "bill",
        "push_to_tally",
    }
)

ApproverPhones = Callable[[], Awaitable[Iterable[str]]]


class Role(str, Enum):
    """Server-side roles from ADR-0007."""

    ADMIN = "admin"
    OPERATOR = "operator"
    APPROVER = "approver"
    VIEWER = "viewer"


ADMIN_ROLES = (Role.ADMIN,)
APPROVER_ROLES = (Role.APPROVER,)
READ_ROLES = (Role.ADMIN, Role.OPERATOR, Role.APPROVER, Role.VIEWER)


class AuthError(Exception):
    """Base authorization failure; ``status_code`` maps it to HTTP."""

    status_code = 401


class UnauthorizedError(AuthError):
    status_code = 401


class ForbiddenError(AuthError):
    status_code = 403


class ConfirmationRequired(AuthError):
    status_code = 409


class SetupUnavailable(AuthError):
    status_code = 409


class SetupNotConfigured(AuthError):
    status_code = 503


@dataclass(frozen=True)
class Principal:
    """The authenticated caller; identity never comes from request bodies."""

    id: str
    name: str
    role: Role
    phone: str | None = None


@dataclass(frozen=True)
class InstanceProfile:
    business_name: str
    timezone: str
    created_at: str


@dataclass(frozen=True)
class KeyRecord:
    id: str
    name: str
    role: Role
    phone: str | None
    key_hash: str
    salt: str
    prefix: str
    created_at: str
    revoked_at: str | None


@dataclass(frozen=True)
class KeySummary:
    id: str
    name: str
    role: Role
    phone: str | None
    prefix: str
    created_at: str
    revoked_at: str | None

    @classmethod
    def from_record(cls, record: KeyRecord) -> KeySummary:
        return cls(
            id=record.id,
            name=record.name,
            role=record.role,
            phone=record.phone,
            prefix=record.prefix,
            created_at=record.created_at,
            revoked_at=record.revoked_at,
        )


@dataclass(frozen=True)
class IssuedKey:
    """The one-time response: ``token`` is never stored or shown again."""

    key_id: str
    name: str
    role: Role
    token: str


class AuthStore(Protocol):
    """Persistence port shared by the CLI, REST, and the MCP gateway."""

    async def init_schema(self) -> None: ...

    async def create_instance(
        self, *, business_name: str, timezone: str
    ) -> InstanceProfile: ...

    async def get_instance(self) -> InstanceProfile | None: ...

    async def update_instance(
        self, *, business_name: str, timezone: str
    ) -> InstanceProfile: ...

    async def insert_key(self, record: KeyRecord) -> None: ...

    async def key_by_prefix(self, prefix: str) -> list[KeyRecord]: ...

    async def get_key(self, key_id: str) -> KeyRecord | None: ...

    async def list_keys(self) -> list[KeyRecord]: ...

    async def revoke_key(self, key_id: str, *, revoked_at: str) -> bool: ...

    async def set_integration(self, service: str, values: dict[str, str]) -> None: ...

    async def get_integration(self, service: str) -> dict[str, str] | None: ...

    async def list_integrations(self) -> list[str]: ...


class InMemoryAuthStore:
    """The test/double adapter for the :class:`AuthStore` port."""

    def __init__(self) -> None:
        self.keys: dict[str, KeyRecord] = {}
        self.instance: InstanceProfile | None = None
        self.integrations: dict[str, dict[str, str]] = {}

    async def init_schema(self) -> None:
        return None

    async def create_instance(
        self, *, business_name: str, timezone: str
    ) -> InstanceProfile:
        if self.instance is None:
            self.instance = InstanceProfile(
                business_name=business_name,
                timezone=timezone,
                created_at=_now(),
            )
        return self.instance

    async def get_instance(self) -> InstanceProfile | None:
        return self.instance

    async def update_instance(
        self, *, business_name: str, timezone: str
    ) -> InstanceProfile:
        if self.instance is None:
            return await self.create_instance(
                business_name=business_name, timezone=timezone
            )
        self.instance = InstanceProfile(
            business_name=business_name,
            timezone=timezone,
            created_at=self.instance.created_at,
        )
        return self.instance

    async def insert_key(self, record: KeyRecord) -> None:
        self.keys[record.id] = record

    async def key_by_prefix(self, prefix: str) -> list[KeyRecord]:
        return [record for record in self.keys.values() if record.prefix == prefix]

    async def get_key(self, key_id: str) -> KeyRecord | None:
        return self.keys.get(key_id)

    async def list_keys(self) -> list[KeyRecord]:
        return list(self.keys.values())

    async def revoke_key(self, key_id: str, *, revoked_at: str) -> bool:
        record = self.keys.get(key_id)
        if record is None:
            return False
        self.keys[key_id] = replace(record, revoked_at=revoked_at)
        return True

    async def set_integration(self, service: str, values: dict[str, str]) -> None:
        self.integrations[service] = dict(values)

    async def get_integration(self, service: str) -> dict[str, str] | None:
        values = self.integrations.get(service)
        return dict(values) if values is not None else None

    async def list_integrations(self) -> list[str]:
        return sorted(self.integrations)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(token: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", token.encode("utf-8"), salt.encode("ascii"), PBKDF2_ITERATIONS
    ).hex()


def bearer_token(header: str | None) -> str | None:
    if not header or not header.startswith("Bearer "):
        return None
    return header[len("Bearer ") :] or None


class AuthService:
    """Issue, verify, revoke, and authorize API keys for one Instance."""

    def __init__(
        self,
        store: AuthStore,
        *,
        approver_phones: ApproverPhones | None = None,
        setup_token: str = "",
    ) -> None:
        self._store = store
        self._approver_phones = approver_phones
        self._setup_token = setup_token
        self._setup_lock = asyncio.Lock()

    async def init_schema(self) -> None:
        await self._store.init_schema()

    async def create_instance(
        self, *, business_name: str, timezone: str
    ) -> InstanceProfile:
        return await self._store.create_instance(
            business_name=business_name, timezone=timezone
        )

    async def get_instance(self) -> InstanceProfile | None:
        return await self._store.get_instance()

    async def update_instance(
        self, *, business_name: str, timezone: str
    ) -> InstanceProfile:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        if not business_name.strip():
            raise ValueError("a business name is required")
        try:
            ZoneInfo(timezone)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError(f"unknown timezone {timezone!r}") from error
        return await self._store.update_instance(
            business_name=business_name, timezone=timezone
        )

    async def has_admin_key(self) -> bool:
        return any(
            record.role is Role.ADMIN for record in await self._store.list_keys()
        )

    async def setup_available(self) -> bool:
        return bool(self._setup_token) and not await self.has_admin_key()

    async def bootstrap(
        self,
        *,
        setup_token: str,
        business_name: str,
        timezone: str = "UTC",
        admin_name: str = "first-admin",
    ) -> IssuedKey:
        if not self._setup_token:
            raise SetupNotConfigured("one-time setup is not configured")
        if not hmac.compare_digest(setup_token, self._setup_token):
            raise UnauthorizedError("invalid setup token")
        async with self._setup_lock:
            if await self.has_admin_key():
                raise SetupUnavailable(
                    "one-time setup is disabled after the first admin key"
                )
            await self.create_instance(business_name=business_name, timezone=timezone)
            return await self.issue_admin_key(name=admin_name)

    async def issue_admin_key(self, *, name: str) -> IssuedKey:
        return await self.issue_key(name=name, role=Role.ADMIN)

    async def issue_key(
        self, *, name: str, role: Role, phone: str | None = None
    ) -> IssuedKey:
        role = Role(role)
        if not name.strip():
            raise ValueError("a key name is required")
        if role is Role.APPROVER:
            if not phone:
                raise ValueError("an approver key requires a phone")
            allowlist = await self._allowlist()
            if phone not in allowlist:
                raise ValueError("approver phone is not allowlisted")
        elif phone:
            raise ValueError("only approver keys carry a phone")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        salt = secrets.token_hex(16)
        record = KeyRecord(
            id=secrets.token_hex(8),
            name=name,
            role=role,
            phone=phone,
            key_hash=_digest(token, salt),
            salt=salt,
            prefix=token[:LOOKUP_PREFIX_LENGTH],
            created_at=_now(),
            revoked_at=None,
        )
        await self._store.insert_key(record)
        return IssuedKey(
            key_id=record.id, name=record.name, role=record.role, token=token
        )

    async def authenticate(self, token: str | None) -> Principal | None:
        if not token or not token.startswith(TOKEN_PREFIX):
            return None
        candidates = await self._store.key_by_prefix(token[:LOOKUP_PREFIX_LENGTH])
        matched: KeyRecord | None = None
        for record in candidates:
            digest = _digest(token, record.salt)
            if (
                hmac.compare_digest(digest, record.key_hash)
                and record.revoked_at is None
            ):
                matched = record
        if matched is None:
            return None
        return Principal(
            id=matched.id, name=matched.name, role=matched.role, phone=matched.phone
        )

    async def authenticate_header(self, header: str | None) -> Principal | None:
        return await self.authenticate(bearer_token(header))

    async def revoke_key(self, key_id: str) -> None:
        record = await self._store.get_key(key_id)
        if record is None:
            raise UnauthorizedError("unknown key")
        if record.role is Role.ADMIN and record.revoked_at is None:
            active_admins = [
                item
                for item in await self._store.list_keys()
                if item.role is Role.ADMIN and item.revoked_at is None
            ]
            if len(active_admins) == 1:
                raise ForbiddenError("cannot revoke the last active admin key")
        await self._store.revoke_key(key_id, revoked_at=_now())

    async def list_keys(self) -> list[KeySummary]:
        return [
            KeySummary.from_record(record) for record in await self._store.list_keys()
        ]

    async def set_integration(self, service: str, values: dict[str, str]) -> None:
        if service not in INTEGRATION_SERVICES:
            raise ValueError(f"unknown integration service {service!r}")
        if not isinstance(values, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in values.items()
        ):
            raise ValueError("integration values must be strings")
        allowed = INTEGRATION_FIELDS[service]
        unknown = [key for key in values if key not in allowed]
        if unknown:
            raise ValueError(f"unknown field(s) for {service}: {', '.join(unknown)}")
        await self._store.set_integration(service, values)

    async def integration_names(self) -> list[str]:
        return await self._store.list_integrations()

    async def integration_url(self, service: str) -> str:
        """Return a URL-bearing Integration's endpoint, never its credentials.

        The Tally push needs only the endpoint; exposing the whole credentials
        dict (which for other services holds secrets) would widen the read
        surface beyond what any caller here needs.
        """
        if (
            service not in INTEGRATION_SERVICES
            or "url" not in INTEGRATION_FIELDS[service]
        ):
            raise ValueError(f"integration {service!r} has no endpoint URL")
        values = await self._store.get_integration(service)
        return (values or {}).get("url", "").strip()

    async def integration_values(self, service: str) -> dict[str, str]:
        """Return a copy of an Integration's stored values, or ``{}`` if absent.

        Unlike :meth:`integration_url` this exposes secrets, so it is reserved
        for trusted channel adapters that need the full credential set (the
        Meta webhook resolves its verify token, app secret, access token and
        phone number id through here). No REST or MCP route returns these
        values; callers must never log them.
        """
        if service not in INTEGRATION_SERVICES:
            raise ValueError(f"unknown integration service {service!r}")
        values = await self._store.get_integration(service)
        return dict(values) if values else {}

    async def integration_health(self) -> dict[str, dict[str, bool]]:
        """Report configured/healthy per Integration, never the secret values.

        "Healthy" here means the required credentials for the service are
        present on this Instance; active provider probes belong to
        ``valence doctor``. A service with no stored credentials is reported as
        unconfigured rather than omitted, so the whole picture is visible.
        """
        configured = set(await self.integration_names())
        report: dict[str, dict[str, bool]] = {}
        for service in sorted(INTEGRATION_SERVICES):
            values = (
                await self._store.get_integration(service)
                if service in configured
                else None
            )
            healthy = bool(values) and all(
                (values or {}).get(field, "").strip()
                for field in REQUIRED_INTEGRATION_FIELDS[service]
            )
            report[service] = {
                "configured": values is not None,
                "healthy": healthy,
            }
        return report

    async def authorize(
        self,
        principal: Principal,
        *,
        roles: Collection[Role],
        action: str | None = None,
        confirm: bool = False,
    ) -> Principal:
        if principal.role not in roles:
            raise ForbiddenError(
                f"role {principal.role.value} cannot perform this action"
            )
        if principal.role is Role.APPROVER:
            allowlist = await self._allowlist()
            if not principal.phone or principal.phone not in allowlist:
                raise ForbiddenError("approver phone is no longer allowlisted")
        if action in CONFIRMED_ACTIONS and not confirm:
            raise ConfirmationRequired(f"action {action!r} requires confirm: true")
        return principal

    async def require(
        self,
        token: str | None,
        *,
        roles: Collection[Role] = READ_ROLES,
        action: str | None = None,
        confirm: bool = False,
    ) -> Principal:
        principal = await self.authenticate(token)
        if principal is None:
            raise UnauthorizedError("invalid or revoked API key")
        return await self.authorize(
            principal, roles=roles, action=action, confirm=confirm
        )

    async def _allowlist(self) -> frozenset[str]:
        if self._approver_phones is None:
            return frozenset()
        return frozenset(await self._approver_phones())
