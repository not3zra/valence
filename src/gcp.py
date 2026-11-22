"""Optional Google Cloud adapters, loaded only when the ``[gcp]`` extra exists.

The default install runs without Google Cloud: Firestore, Cloud Storage, and
the ADK Firestore session service are optional adapters (ADR-0006). Every
import of ``google.cloud`` lives behind this module so a missing extra fails at
the adapter boundary with an actionable message instead of breaking the
application at import time.
"""

from __future__ import annotations

from typing import Any

_GCP_EXTRA_HINT = (
    "Google Cloud support is optional. Install it with: "
    "pip install 'valence-order-agent[gcp]'"
)


class GcpExtraRequired(RuntimeError):
    """Raised when a GCP adapter is selected without the optional extra."""


def require_firestore() -> Any:
    """Import ``google.cloud.firestore`` or explain how to install the extra."""
    try:
        from google.cloud import firestore
    except ImportError as exc:
        raise GcpExtraRequired(_GCP_EXTRA_HINT) from exc
    return firestore


def require_storage() -> Any:
    """Import ``google.cloud.storage`` or explain how to install the extra."""
    try:
        from google.cloud import storage  # type: ignore[attr-defined]
    except ImportError as exc:
        raise GcpExtraRequired(_GCP_EXTRA_HINT) from exc
    return storage


def create_firestore_client(*, database: str | None = None) -> Any:
    """Create a Firestore async client on the current event loop.

    ``google.api_core`` caches gRPC channels by target, so a channel created on
    one loop can be reused on another and fail with "attached to a different
    loop". Clearing the cache first gives the client a fresh channel bound to
    the calling loop.
    """
    firestore = require_firestore()
    try:
        import google.api_core.grpc_helpers_async as _gh

        if hasattr(_gh, "_channel_cache"):
            _gh._channel_cache.clear()
    except Exception:
        pass
    if database is None:
        return firestore.AsyncClient()
    return firestore.AsyncClient(database=database)
