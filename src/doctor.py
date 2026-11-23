"""Pre-flight checks for one Valence Instance (``valence doctor``, issue #97).

Every check is local and read-only: doctor connects to the configured database,
resolves the configured adapters and reports pass/fail without contacting model
or integration providers. Details carry only safe values — URLs are reduced to
their driver, and credentials are reported as present/absent, never printed.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from .auth import AuthService
from .config import settings
from .model_provider import build_model_provider
from .sql_store import SqlAuthStore
from .voucher import (
    FilesystemVoucherStore,
    GcsVoucherStore,
    default_voucher_storage,
)

SUPPORTED_SCHEMES = ("sqlite+aiosqlite://", "postgresql+asyncpg://")
_STORE_KINDS = frozenset({"memory", "sqlite", "postgres", "firestore"})


@dataclass(frozen=True)
class CheckResult:
    """One doctor check, safe to print or serialise verbatim."""

    name: str
    ok: bool
    detail: str


def redact_database_url(url: str) -> str:
    """Identify a database driver without disclosing host or credentials."""
    try:
        return f"{make_url(url).get_backend_name()} (credentials redacted)"
    except Exception:
        return "<redacted>"


def is_supported_database_url(url: str) -> bool:
    """True when a URL uses one of the two supported async SQL drivers."""
    return url.startswith(SUPPORTED_SCHEMES)


def require_database_url(url: str) -> None:
    """Reject an unsupported URL without echoing its credentials."""
    if not is_supported_database_url(url):
        raise SystemExit(
            "unsupported DATABASE_URL; use sqlite+aiosqlite:// or "
            "postgresql+asyncpg://"
        )


def format_checks(checks: list[CheckResult]) -> str:
    """Render check results as human-readable PASS/FAIL lines."""
    return "".join(
        f"{'PASS' if check.ok else 'FAIL'} {check.name}: {check.detail}\n"
        for check in checks
    )


def as_json(checks: list[CheckResult]) -> str:
    """Render check results as a machine-readable JSON array."""
    import json

    return json.dumps([asdict(check) for check in checks], indent=2) + "\n"


def _check_python() -> CheckResult:
    current = sys.version_info
    ok = (current.major, current.minor) >= (3, 12)
    detail = f"Python {current.major}.{current.minor}.{current.micro}"
    if not ok:
        detail += "; 3.12 or newer is required"
    return CheckResult("python", ok, detail)


def _check_configuration(url: str) -> CheckResult:
    problems: list[str] = []
    if not is_supported_database_url(url):
        problems.append(
            "DATABASE_URL must use sqlite+aiosqlite:// or postgresql+asyncpg://"
        )
    for name in ("order_store", "session_service"):
        value = getattr(settings, name)
        if value and value not in _STORE_KINDS:
            problems.append(f"unsupported {name.upper()}")
    if settings.order_store == "postgres" and not url.startswith(
        "postgresql+asyncpg://"
    ):
        problems.append("ORDER_STORE=postgres requires a postgresql+asyncpg:// URL")
    numeric = {
        "PORT": settings.service_port,
        "WEBHOOK_RATE_LIMIT_PER_SENDER": settings.webhook_rate_limit,
        "AGENT_TURN_TIMEOUT_S": settings.agent_turn_timeout_s,
        "AGENT_MAX_CONCURRENT_TURNS": settings.agent_max_concurrent_turns,
    }
    for name, value in numeric.items():
        if value <= 0:
            problems.append(f"{name} must be positive")
    if settings.service_port > 65535:
        problems.append("PORT must be at most 65535")
    detail = f"database={redact_database_url(url)}"
    if problems:
        return CheckResult("configuration", False, f"{detail}; {'; '.join(problems)}")
    return CheckResult("configuration", True, detail)


async def _check_database(engine: AsyncEngine | None, url: str) -> CheckResult:
    if engine is None:
        return CheckResult(
            "database", False, "DATABASE_URL uses an unsupported driver"
        )
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as error:
        return CheckResult(
            "database", False, f"{type(error).__name__}: connection failed"
        )
    return CheckResult(
        "database", True, f"connected via {redact_database_url(url)}"
    )


def _expected_heads() -> tuple[str, ...]:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "migrations")
    )
    return tuple(ScriptDirectory.from_config(config).get_heads())


async def _current_heads(engine: AsyncEngine) -> tuple[str, ...]:
    async with engine.connect() as connection:
        return tuple(
            await connection.run_sync(
                lambda sync: MigrationContext.configure(sync).get_current_heads()
            )
        )


async def _check_migrations(engine: AsyncEngine | None) -> CheckResult:
    if engine is None:
        return CheckResult(
            "migrations", False, "database unavailable; migrations not checked"
        )
    try:
        expected = _expected_heads()
        current = await _current_heads(engine)
    except Exception as error:
        return CheckResult(
            "migrations", False, f"{type(error).__name__}: could not read revisions"
        )
    if set(current) != set(expected):
        return CheckResult(
            "migrations",
            False,
            f"head={', '.join(expected) or 'none'} current="
            f"{', '.join(current) or 'none'}; run 'alembic upgrade head'",
        )
    return CheckResult(
        "migrations", True, f"current={', '.join(current) or 'base'}"
    )


async def _has_model_credentials(
    auth: AuthService | None, env: Mapping[str, str]
) -> bool:
    if any(env.get(name, "").strip() for name in ("GOOGLE_API_KEY", "GEMINI_API_KEY")):
        return True
    if env.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() == "true" and env.get(
        "GOOGLE_CLOUD_LOCATION", ""
    ).strip():
        return True
    if auth is None:
        return False
    try:
        values = await auth.integration_values("gemini")
    except Exception:
        return False
    return bool((values.get("api_key") or "").strip())


async def _check_model_provider(
    auth: AuthService | None, env: Mapping[str, str]
) -> CheckResult:
    try:
        provider = build_model_provider()
        model = getattr(provider, "model", "")
        model_name = model if isinstance(model, str) else type(model).__name__
    except ValueError as error:
        return CheckResult("model provider", False, str(error))
    if settings.model_provider == "fake":
        return CheckResult(
            "model provider", True, f"provider=fake model={model_name}"
        )
    if not await _has_model_credentials(auth, env):
        return CheckResult(
            "model provider",
            False,
            f"provider={settings.model_provider} model={model_name}; "
            "credentials not set",
        )
    return CheckResult(
        "model provider",
        True,
        f"provider={settings.model_provider} model={model_name}; credentials set",
    )


def _check_voucher_store() -> CheckResult:
    storage = default_voucher_storage(
        bucket=settings.voucher_bucket, directory=settings.voucher_dir
    )
    if isinstance(storage, GcsVoucherStore):
        try:
            from .gcp import require_storage

            require_storage()
        except ImportError:
            return CheckResult(
                "voucher store",
                False,
                "GCS bucket configured but the [gcp] extra is missing",
            )
        return CheckResult("voucher store", True, "backend=Cloud Storage")
    if isinstance(storage, FilesystemVoucherStore):
        root = Path(storage.root)
        if not root.is_dir() or not os.access(root, os.W_OK):
            return CheckResult(
                "voucher store",
                False,
                "VOUCHER_DIR is not an existing writable directory",
            )
        return CheckResult("voucher store", True, "backend=filesystem (writable)")
    return CheckResult("voucher store", True, "backend=in-memory (not durable)")


async def _check_tally(auth: AuthService | None) -> CheckResult:
    if auth is None:
        return CheckResult(
            "tally", False, "database unavailable; Tally config not checked"
        )
    try:
        health = await auth.integration_health()
        tally_url = (await auth.integration_url("tally")).strip()
    except Exception:
        return CheckResult("tally", False, "could not read Tally configuration")
    legacy_url = settings.tally_push_url.strip()
    if (
        health.get("tally", {}).get("healthy")
        or tally_url.startswith(("http://", "https://"))
        or legacy_url.startswith(("http://", "https://"))
    ):
        source = (
            "Integration"
            if tally_url
            else "TALLY_PUSH_URL"
            if legacy_url
            else "Instance config"
        )
        return CheckResult("tally", True, f"push URL configured via {source}")
    return CheckResult("tally", False, "no Tally push URL configured")


async def _check_whatsapp(
    auth: AuthService | None, env: Mapping[str, str]
) -> CheckResult:
    """Check the Meta Integration used by the WhatsApp Source Channel."""
    if auth is not None:
        try:
            health = await auth.integration_health()
        except Exception:
            health = {}
        if health.get("meta", {}).get("healthy"):
            return CheckResult("whatsapp", True, "Meta Integration configured")
    if env.get("META_ACCESS_TOKEN", "").strip():
        return CheckResult("whatsapp", True, "Meta access token set in environment")
    return CheckResult("whatsapp", False, "no Meta credentials configured")


def _check_mcp() -> CheckResult:
    try:
        from .mcp_app import create_mcp_server
        from .mcp_server import MCP_TOOL_NAMES

        create_mcp_server()
    except Exception as error:
        return CheckResult(
            "mcp", False, f"{type(error).__name__}: server could not be built"
        )
    return CheckResult(
        "mcp",
        True,
        f"streamable HTTP at /mcp; {len(MCP_TOOL_NAMES)} tools",
    )


async def run_doctor(
    url: str | None = None, *, env: Mapping[str, str] | None = None
) -> list[CheckResult]:
    """Run every check against the configured Instance, in dependency order."""
    url = url or settings.database_url
    env = os.environ if env is None else env
    engine: AsyncEngine | None = (
        create_async_engine(url, poolclass=NullPool)
        if is_supported_database_url(url)
        else None
    )
    auth = AuthService(SqlAuthStore(engine=engine)) if engine is not None else None
    try:
        return [
            _check_python(),
            _check_configuration(url),
            await _check_database(engine, url),
            await _check_migrations(engine),
            await _check_model_provider(auth, env),
            _check_voucher_store(),
            await _check_tally(auth),
            await _check_whatsapp(auth, env),
            _check_mcp(),
        ]
    finally:
        if engine is not None:
            await engine.dispose()


__all__ = [
    "SUPPORTED_SCHEMES",
    "CheckResult",
    "as_json",
    "format_checks",
    "is_supported_database_url",
    "redact_database_url",
    "require_database_url",
    "run_doctor",
]
