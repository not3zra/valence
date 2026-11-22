"""``valence`` CLI: the one direct-DB bootstrap path (issue #57, ADR-0009).

``valence init`` creates the schema, the Instance profile, and the first admin
key from an empty database. It is idempotent: an existing admin key is never
overwritten or duplicated, so it cannot compete with the developer boot (T04).
Everything after the first key belongs to the admin API.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from .auth import AuthService
from .config import settings
from .sql_store import SqlAuthStore, SqlOrderStore

_SUPPORTED_SCHEMES = ("sqlite+aiosqlite://", "postgresql+asyncpg://")


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
    return parser


async def _init(args: argparse.Namespace) -> int:
    url = args.database_url or settings.database_url
    if not url.startswith(_SUPPORTED_SCHEMES):
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
            import json

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


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns a process exit code."""
    args = _parser().parse_args(argv)
    if args.command == "init":
        return asyncio.run(_init(args))
    raise SystemExit(f"unknown command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
