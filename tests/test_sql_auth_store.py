"""SQL adapter for the bootstrap/auth tables (issue #57)."""

from __future__ import annotations

from src.auth import AuthService, InMemoryAuthStore, Role
from src.sql_store import SqlAuthStore, SqlOrderStore


def _url(tmp_path):
    return f"sqlite+aiosqlite:///{tmp_path / 'auth.db'}"


async def test_sql_auth_store_round_trips_keys_and_instance(tmp_path):
    store = SqlAuthStore(_url(tmp_path))
    await store.init_schema()
    service = AuthService(store)
    await service.create_instance(business_name="Acme", timezone="Asia/Kolkata")
    issued = await service.issue_key(name="operator", role=Role.OPERATOR)

    reloaded = AuthService(SqlAuthStore(_url(tmp_path)))
    principal = await reloaded.authenticate(issued.token)

    assert principal is not None
    assert principal.role is Role.OPERATOR
    assert (await reloaded.get_instance()).business_name == "Acme"
    assert await reloaded.has_admin_key() is False


async def test_revocation_and_integration_credentials_persist(tmp_path):
    url = _url(tmp_path)
    store = SqlAuthStore(url)
    await store.init_schema()
    service = AuthService(store)
    admin = await service.issue_admin_key(name="first-admin")
    operator = await service.issue_key(name="operator", role=Role.OPERATOR)
    await service.set_integration("meta", {"access_token": "provider-secret"})

    await service.revoke_key(operator.key_id)
    reloaded = AuthService(SqlAuthStore(url))

    assert await reloaded.authenticate(operator.token) is None
    assert (await reloaded.authenticate(admin.token)) is not None
    assert (await reloaded.integration_names()) == ["meta"]
    assert (await reloaded._store.get_integration("meta"))["access_token"] == (
        "provider-secret"
    )


def test_production_auth_reuses_the_runtime_engine(tmp_path):
    from src.main import _auth_store

    class _Runtime:
        def __init__(self, store):
            self.store = store

    orders = SqlOrderStore(_url(tmp_path))
    assert _auth_store(_Runtime(orders)).engine is orders.engine


def test_sql_adapter_satisfies_the_same_port_as_the_double():
    port_methods = (
        "init_schema",
        "create_instance",
        "get_instance",
        "insert_key",
        "key_by_prefix",
        "get_key",
        "list_keys",
        "revoke_key",
        "set_integration",
        "get_integration",
        "list_integrations",
    )
    assert all(hasattr(SqlAuthStore, name) for name in port_methods)
    assert all(hasattr(InMemoryAuthStore, name) for name in port_methods)
