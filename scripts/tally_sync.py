#!/usr/bin/env python3
"""Local Tally sync script.

Run this on your PC to automatically push prepared vouchers to your local
Tally instance. It polls the Valence Agent Interface for orders with prepared
vouchers and pushes their XML to Tally via HTTP import.

Usage:
    python scripts/tally_sync.py

Environment variables:
    VALENCE_SERVICE_URL  - The Valence service URL (required)
    VALENCE_API_KEY      - An approver-role Agent Interface key (required)
    TALLY_URL            - Your local Tally import URL (default: http://localhost:9000)
"""

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote


# Config
def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} must be set")
    return value


def _require_http_url(name: str, value: str) -> str:
    if not value.startswith(("http://", "https://")):
        raise SystemExit(f"{name} must start with http:// or https://")
    return value


SERVICE_URL = ""
API_KEY = ""
TALLY_URL = os.environ.get("TALLY_URL", "http://localhost:9000")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
STATE_FILE = Path(__file__).parent / ".tally_sync_state.json"


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"pushed": []}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def fetch_vouchers() -> list[dict]:
    """List orders that carry a prepared voucher through the v1 Agent Interface.

    ``GET /api/v1/orders`` already exposes each order's ``voucher_id``, so the
    retired ``/api/vouchers`` aggregate needs no replacement route — the script
    projects the same polling shape from the versioned order listing.
    """
    req = urllib.request.Request(
        f"{SERVICE_URL}/api/v1/orders",
        headers={"Authorization": f"Bearer {API_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        orders = json.loads(resp.read())
    return [
        {
            "order_id": order["order_id"],
            "voucher_id": order["voucher_id"],
            "customer": order["customer"],
            "total": order["draft_value_inr"],
            "status": order["status"],
        }
        for order in orders
        if order.get("voucher_id")
    ]


def fetch_voucher_xml(order_id: str) -> str:
    """Download a prepared order's voucher XML through the v1 Agent Interface."""
    req = urllib.request.Request(
        f"{SERVICE_URL}/api/v1/orders/{quote(order_id, safe='')}/voucher",
        headers={"Authorization": f"Bearer {API_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode()


def push_to_tally(xml: str) -> dict:
    """Push XML to the local Tally import endpoint."""
    req = urllib.request.Request(
        TALLY_URL,
        data=xml.encode("utf-8"),
        headers={"Content-Type": "application/xml"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode(errors="replace")
            return {"ok": True, "status": resp.status, "response": body[:500]}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:200]
        return {"ok": False, "error": f"HTTP {exc.code}: {detail}"}
    except (OSError, urllib.error.URLError) as exc:
        return {"ok": False, "error": str(exc)}


def poll():
    """Poll for new vouchers and push them to Tally."""
    state = load_state()
    pushed = set(state.get("pushed", []))

    try:
        vouchers = fetch_vouchers()
    except Exception as exc:
        print(f"[error] Failed to fetch vouchers: {exc}", flush=True)
        return

    new_vouchers = [v for v in vouchers if v["order_id"] not in pushed]
    if not new_vouchers:
        return

    for v in new_vouchers:
        order_id = v["order_id"]
        print(
            f"[sync] Found new voucher: {order_id} "
            f"(customer={v['customer']}, total={v['total']})",
            flush=True,
        )

        try:
            xml = fetch_voucher_xml(order_id)
        except Exception as exc:
            print(
                f"[error] Failed to download voucher XML for {order_id}: {exc}",
                flush=True,
            )
            continue

        result = push_to_tally(xml)
        if result["ok"]:
            resp = result.get("response", "")
            if "LINEERROR" in resp:
                print(f"[error] Tally rejected {order_id}: {resp[:300]}", flush=True)
            else:
                print(
                    f"[ok] Pushed {order_id} to Tally (status={result['status']})",
                    flush=True,
                )
                pushed.add(order_id)
                state["pushed"] = list(pushed)
                save_state(state)
        else:
            print(
                f"[error] Failed to push {order_id} to Tally: {result['error']}",
                flush=True,
            )


def main():
    global SERVICE_URL, API_KEY
    SERVICE_URL = _require_http_url(
        "VALENCE_SERVICE_URL", _required_env("VALENCE_SERVICE_URL").rstrip("/")
    )
    API_KEY = _required_env("VALENCE_API_KEY")
    _require_http_url("TALLY_URL", TALLY_URL)

    print("[tally_sync] Starting Tally sync", flush=True)
    print(f"  Service: {SERVICE_URL}", flush=True)
    print(f"  Tally:   {TALLY_URL}", flush=True)
    print(f"  Poll:    every {POLL_INTERVAL}s", flush=True)

    while True:
        poll()
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
