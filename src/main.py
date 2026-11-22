"""Production entry point: the composition root selects the configured adapters.

``build_runtime`` resolves the store, session service, voucher store, and model
provider from configuration; the default install needs no Google Cloud at all.
Changing an adapter is a configuration change in one module, not a rewrite of
the web layer or the executor. The auth service shares the order store's
approver allowlist and is the single authorization layer for REST and MCP.
"""

from __future__ import annotations

import uvicorn

from .admin import AdminService
from .auth import AuthService
from .config import settings
from .runtime import build_runtime
from .sql_store import SqlAuthStore, SqlOrderStore
from .web import create_app


def _approver_phones(runtime):
    async def phones() -> list[str]:
        store = runtime.store
        if store is None:
            return []
        return [approver.phone for approver in await store.get_approvers()]

    return phones


def _auth_store(runtime) -> SqlAuthStore:
    """Reuse the runtime's engine so each Instance has one connection pool."""
    store = runtime.store
    if isinstance(store, SqlOrderStore):
        return SqlAuthStore(engine=store.engine)
    return SqlAuthStore(settings.database_url)


def create_valence_app():
    runtime = build_runtime()
    auth = AuthService(
        _auth_store(runtime),
        setup_token=settings.setup_token,
        approver_phones=_approver_phones(runtime),
    )
    return create_app(
        runtime=runtime,
        auth=auth,
        admin=AdminService(runtime.store) if runtime.store is not None else None,
    )


app = create_valence_app()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.service_port)
