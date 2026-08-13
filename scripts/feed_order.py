"""Feed a structured order through the Order Processing Core with no channel.

Lets a developer drive the core directly — no agent, no Twilio, no webhook.
Reads a JSON order from a file (or stdin), persists it through the store, and
prints the core's decision. Uses the real Firestore store (emulator when
``FIRESTORE_EMULATOR_HOST`` is set) unless ``--memory`` is given.

Usage:
    python scripts/feed_order.py order.json
    python scripts/feed_order.py --memory order.json
    echo '{"phone": "+919812345001", ...}' | python scripts/feed_order.py --memory
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from src.core import OrderProcessingCore
from src.orders import Order, OrderItem
from src.store import FirestoreOrderStore, InMemoryOrderStore


def _parse_order(data: dict) -> Order:
    return Order(
        phone=str(data.get("phone", "")),
        customer=data.get("customer"),
        delivery_location=data.get("delivery_location"),
        confidence=float(data.get("confidence", 0.0)),
        source_channel=str(data.get("source_channel", "whatsapp")),
        source_language=str(data.get("source_language", "en")),
        items=[OrderItem.from_dict(item) for item in data.get("items", [])],
    )


async def _main(args: argparse.Namespace) -> int:
    raw = open(args.path, encoding="utf-8").read() if args.path else sys.stdin.read()
    data = json.loads(raw)
    store = InMemoryOrderStore() if args.memory else FirestoreOrderStore()
    decision = await OrderProcessingCore(store).process(_parse_order(data))
    print(json.dumps(decision.to_dict(), indent=2, ensure_ascii=False))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Feed an order JSON through the Order Processing Core."
    )
    parser.add_argument(
        "path", nargs="?", help="path to an order JSON file (else stdin)"
    )
    parser.add_argument(
        "--memory",
        action="store_true",
        help="run against the seed data in memory instead of Firestore",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
