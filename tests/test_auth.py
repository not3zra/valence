"""Server-side API key lifecycle: hashed at rest, shown once, revocable (issue #57)."""

from __future__ import annotations

import asyncio

import pytest

from src.auth import (
    AuthService,
    ConfirmationRequired,
    ForbiddenError,
    InMemoryAuthStore,
    IssuedKey,
    Role,
    SetupUnavailable,
    UnauthorizedError,
)


async def test_issued_key_is_shown_once_and_hashed_at_rest():
    store = InMemoryAuthStore()
    service = AuthService(store)

    issued = await service.issue_admin_key(name="first-admin")

    assert issued.token.startswith("vk_")
    stored = await store.get_key(issued.key_id)
    assert stored is not None
    assert stored.key_hash != issued.token
    assert issued.token not in stored.key_hash
    assert await service.authenticate(issued.token) is not None


async def test_revoked_key_can_no_longer_authenticate():
    store = InMemoryAuthStore()
    service = AuthService(store)
    await service.issue_admin_key(name="first-admin")
    issued = await service.issue_key(name="temp-operator", role=Role.OPERATOR)

    await service.revoke_key(issued.key_id)

    assert await service.authenticate(issued.token) is None
    assert await store.get_key(issued.key_id) is not None


async def test_last_active_admin_key_cannot_be_revoked():
    service = AuthService(InMemoryAuthStore())
    admin = await service.issue_admin_key(name="first-admin")

    with pytest.raises(ForbiddenError):
        await service.revoke_key(admin.key_id)


def test_in_memory_store_starts_empty():
    store = InMemoryAuthStore()
    assert store.keys == {}


async def test_roles_are_enforced_server_side():
    service = AuthService(InMemoryAuthStore())
    viewer = await service.issue_key(name="viewer", role=Role.VIEWER)

    with pytest.raises(ForbiddenError):
        await service.require(viewer.token, roles=(Role.ADMIN,))
    assert (await service.require(viewer.token, roles=(Role.VIEWER,))).role is (
        Role.VIEWER
    )


async def test_privileged_action_requires_confirmation():
    service = AuthService(
        InMemoryAuthStore(),
        approver_phones=lambda: _async(["+919812345001"]),
    )
    await service.issue_admin_key(name="first-admin")
    approver = await service.issue_key(
        name="approver", role=Role.APPROVER, phone="+919812345001"
    )

    with pytest.raises(ConfirmationRequired):
        await service.require(approver.token, roles=(Role.APPROVER,), action="approve")
    principal = await service.require(
        approver.token, roles=(Role.APPROVER,), action="approve", confirm=True
    )
    assert principal.phone == "+919812345001"


async def test_approver_keys_bind_to_the_allowlist():
    allowlist = ["+919812345001"]
    service = AuthService(
        InMemoryAuthStore(), approver_phones=lambda: _async(allowlist)
    )

    with pytest.raises(ValueError, match="not allowlisted"):
        await service.issue_key(name="bad", role=Role.APPROVER, phone="+919999999999")
    approver = await service.issue_key(
        name="good", role=Role.APPROVER, phone=allowlist[0]
    )
    allowlist.clear()

    with pytest.raises(ForbiddenError, match="no longer allowlisted"):
        await service.require(approver.token, roles=(Role.APPROVER,))


async def test_one_time_setup_creates_first_admin_then_closes():
    store = InMemoryAuthStore()
    service = AuthService(store, setup_token="setup-secret")

    assert await service.setup_available()
    issued = await service.bootstrap(
        setup_token="setup-secret", business_name="Acme Chemicals"
    )
    assert issued.role is Role.ADMIN
    assert (await service.get_instance()).business_name == "Acme Chemicals"
    assert not await service.setup_available()
    with pytest.raises(SetupUnavailable):
        await service.bootstrap(
            setup_token="setup-secret", business_name="Other Business"
        )
    with pytest.raises(UnauthorizedError):
        await service.bootstrap(setup_token="wrong", business_name="Other Business")


async def test_concurrent_setup_only_creates_one_admin_key():
    service = AuthService(InMemoryAuthStore(), setup_token="setup-secret")

    results = await asyncio.gather(
        *[
            service.bootstrap(
                setup_token="setup-secret", business_name="Acme Chemicals"
            )
            for _ in range(4)
        ],
        return_exceptions=True,
    )

    assert sum(isinstance(item, IssuedKey) for item in results) == 1
    assert sum(isinstance(item, SetupUnavailable) for item in results) == 3
    assert len(await service.list_keys()) == 1


async def test_integration_credentials_are_separate_from_user_keys():
    store = InMemoryAuthStore()
    service = AuthService(store)
    issued = await service.issue_admin_key(name="first-admin")

    await service.set_integration("meta", {"access_token": "provider-secret"})
    await service.set_integration("gemini", {"api_key": "model-secret"})
    await service.set_integration("tally", {"url": "https://tally.local"})

    assert set(await service.integration_names()) == {"meta", "gemini", "tally"}
    assert issued.token not in str(store.integrations)
    assert store.integrations["meta"]["access_token"] == "provider-secret"
    assert all(record.phone is None for record in store.keys.values())


async def test_bearer_header_is_parsed_for_shared_use():
    service = AuthService(InMemoryAuthStore())
    issued = await service.issue_admin_key(name="first-admin")

    assert await service.authenticate_header(f"Bearer {issued.token}") is not None
    assert await service.authenticate_header(issued.token) is None
    assert await service.authenticate_header("Bearer ") is None


async def _async(value):
    return value
