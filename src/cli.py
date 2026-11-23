"""``valence`` CLI: the direct-DB bootstrap and operations path.

``valence init`` creates the schema, the Instance profile, and the first admin
key from an empty database. It is idempotent: an existing admin key is never
overwritten or duplicated, so it cannot compete with the developer boot (T04).
Everything after the first key belongs to the admin API. ``valence doctor``
runs local pre-flight checks and ``valence cutoff`` renders Loading Lists from
the configured database (issue #97); both are read-only and redact secrets.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .api_v1 import MAX_LOADING_LIST_WINDOW_DAYS
from .auth import AuthService
from .config import settings
from .doctor import SUPPORTED_SCHEMES, as_json, format_checks, run_doctor
from .loading_service import LoadingService, parse_delivery_day
from .setup import (
    HttpSetupClient,
    load_fixture,
    run_setup,
    with_integrations,
)
from .sql_store import SqlAuthStore, SqlOrderStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="valence")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser(
        "init", help="create the schema, Instance profile, and first admin key"
    )
    init.add_argument(
        "--database-url",
        default=None,
        help="SQLAlchemy async URL (default: DATABASE_URL)",
    )
    init.add_argument("--business-name", required=True)
    init.add_argument("--timezone", default="UTC")
    init.add_argument("--admin-name", default="first-admin")
    init.add_argument("--json", action="store_true", help="emit a machine-readable key")
    setup = commands.add_parser(
        "setup", help="configure an initialized Instance from a sample fixture"
    )
    setup.add_argument("--url", default=None, help="Instance base URL")
    setup.add_argument("--admin-key", default=None, help="first admin API key")
    setup.add_argument("--fixture", default=None, help="sample fixture directory")
    setup.add_argument(
        "--integrations", default=None, help="JSON file of Integration credentials"
    )
    mcp = commands.add_parser(
        "mcp", help="run the MCP server over stdio or streamable HTTP"
    )
    mcp.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
        help="MCP transport (default: stdio)",
    )
    mcp.add_argument("--host", default="127.0.0.1", help="streamable HTTP host")
    mcp.add_argument("--port", type=int, default=8000, help="streamable HTTP port")
    mcp.add_argument(
        "--api-key", default=None, help="API key for stdio (default: VALENCE_API_KEY)"
    )
    cutoff = commands.add_parser(
        "cutoff",
        help="render a Loading List for a delivery day or window",
        description=(
            "Render the live Loading List for one delivery day (--day) or for every "
            "day in an inclusive window (--start/--end), using the same service as "
            "the v1 API. Output is JSON on stdout unless --output is given. The "
            "render is read-only: no late-order heads-ups are sent."
        ),
    )
    cutoff.add_argument(
        "--database-url",
        default=None,
        help="SQLAlchemy async URL (default: DATABASE_URL)",
    )
    cutoff.add_argument(
        "--day",
        "--date",
        dest="day",
        default=None,
        metavar="YYYY-MM-DD",
        help="single delivery day to render (default: today in business time)",
    )
    cutoff.add_argument(
        "--start",
        default=None,
        metavar="YYYY-MM-DD",
        help="first day of an inclusive window (requires --end)",
    )
    cutoff.add_argument(
        "--end",
        default=None,
        metavar="YYYY-MM-DD",
        help="last day of an inclusive window (requires --start)",
    )
    cutoff.add_argument(
        "--output",
        "-o",
        default=None,
        metavar="PATH",
        help="write the render to a file instead of stdout",
    )
    doctor = commands.add_parser(
        "doctor",
        help="check configuration, database, migrations and active integrations",
        description=(
            "Run local, read-only pre-flight checks against the configured "
            "Instance: Python, configuration, database connectivity and "
            "migrations, model provider, voucher store, Tally and MCP. No "
            "provider is contacted and no secret value is printed."
        ),
    )
    doctor.add_argument(
        "--database-url",
        default=None,
        help="SQLAlchemy async URL (default: DATABASE_URL)",
    )
    doctor.add_argument(
        "--json", action="store_true", help="emit machine-readable check results"
    )
    return parser


async def _init(args: argparse.Namespace) -> int:
    url = args.database_url or settings.database_url
    if not url.startswith(SUPPORTED_SCHEMES):
        raise SystemExit(
            f"unsupported DATABASE_URL {url!r}; use sqlite+aiosqlite:// or "
            "postgresql+asyncpg://"
        )
    orders = SqlOrderStore(url)
    auth = AuthService(SqlAuthStore(engine=orders.engine))
    try:
        await orders.initialize()
        if await auth.get_instance() is None:
            await auth.create_instance(
                business_name=args.business_name, timezone=args.timezone
            )
        if await auth.has_admin_key():
            print("already initialized; no new admin key was issued")
            return 0
        issued = await auth.issue_admin_key(name=args.admin_name)
        if args.json:
            print(
                json.dumps(
                    {
                        "key_id": issued.key_id,
                        "name": issued.name,
                        "role": issued.role.value,
                        "token": issued.token,
                    }
                )
            )
        else:
            print(f"Instance: {args.business_name}")
            print(f"First admin key ({issued.key_id}): {issued.token}")
            print("Copy this token now — it is shown once and cannot be recovered.")
        return 0
    finally:
        await orders.engine.dispose()


def _setup(args: argparse.Namespace) -> int:
    url = args.url or input(
        f"Instance URL [{settings.service_url or 'http://localhost:8080'}]: "
    ).strip() or (settings.service_url or "http://localhost:8080")
    admin_key = args.admin_key or getpass.getpass("Admin API key: ")
    if not admin_key:
        raise SystemExit("an admin key is required; run 'valence init' first")
    fixture_path = args.fixture or input(
        "Fixture directory [examples/acme-chemicals]: "
    ).strip() or "examples/acme-chemicals"
    fixture = load_fixture(fixture_path)
    if args.integrations:
        fixture = with_integrations(fixture, args.integrations)
    steps = run_setup(HttpSetupClient(url, admin_key), fixture)
    for step in steps:
        print(f"configured {step}")
    print("Instance setup complete.")
    return 0


def _mcp(args: argparse.Namespace) -> int:
    from .mcp_app import create_mcp_server
    from .mcp_server import run_mcp_server

    server = create_mcp_server(api_key=args.api_key or settings.valence_api_key)
    run_mcp_server(
        server, transport=args.transport, host=args.host, port=args.port
    )
    return 0


def _validate_cutoff_selectors(
    args: argparse.Namespace,
) -> tuple[date | None, date | None, date | None]:
    """Resolve and validate the day/window selectors shared with the v1 API."""
    if args.day is not None and (args.start is not None or args.end is not None):
        raise SystemExit("supply either day or window, not both")
    if (args.start is None) != (args.end is None):
        raise SystemExit("--start and --end must be supplied together")
    try:
        day = parse_delivery_day(args.day)
        start = parse_delivery_day(args.start)
        end = parse_delivery_day(args.end)
    except ValueError as error:
        raise SystemExit(f"invalid day: {error}") from None
    if start is not None and end is not None:
        if end < start:
            raise SystemExit("window end must not precede start")
        if (end - start).days >= MAX_LOADING_LIST_WINDOW_DAYS:
            raise SystemExit("window is too large")
    return day, start, end


async def _cutoff(args: argparse.Namespace) -> int:
    url = args.database_url or settings.database_url
    if not url.startswith(SUPPORTED_SCHEMES):
        raise SystemExit(
            f"unsupported DATABASE_URL {url!r}; use sqlite+aiosqlite:// or "
            "postgresql+asyncpg://"
        )
    day, start, end = _validate_cutoff_selectors(args)
    store = SqlOrderStore(url)
    try:
        service = LoadingService(store)
        payload: dict[str, Any] | list[dict[str, Any]]
        if start is not None and end is not None:
            payload = [
                await service.render_loading_list(start + timedelta(days=offset))
                for offset in range((end - start).days + 1)
            ]
        else:
            payload = await service.render_loading_list(day)
    finally:
        await store.engine.dispose()
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


def _doctor(args: argparse.Namespace) -> int:
    checks = asyncio.run(run_doctor(args.database_url))
    print((as_json if args.json else format_checks)(checks), end="")
    return 0 if all(check.ok for check in checks) else 1


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns a process exit code."""
    args = _parser().parse_args(argv)
    if args.command == "init":
        return asyncio.run(_init(args))
    if args.command == "setup":
        return _setup(args)
    if args.command == "mcp":
        return _mcp(args)
    if args.command == "cutoff":
        return asyncio.run(_cutoff(args))
    if args.command == "doctor":
        return _doctor(args)
    raise SystemExit(f"unknown command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
