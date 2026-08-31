#!/usr/bin/env python3
"""Local Tally sync script.

Run this on your PC to automatically push prepared vouchers to your local
Tally instance. It polls the Valence service for new vouchers and pushes
them to Tally via HTTP import.

Usage:
    python scripts/tally_sync.py

Environment variables:
    VALENCE_SERVICE_URL  - The Cloud Run service URL
    VALENCE_WEB_PASSCODE - The web passcode for authentication
    TALLY_URL            - Your local Tally import URL (default: http://localhost:9000)
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# Config
SERVICE_URL = os.environ.get("VALENCE_SERVICE_URL", "https://valence-371317348606.us-central1.run.app")
WEB_PASSCODE = os.environ.get("VALENCE_WEB_PASSCODE", "vlnc-49e90d31")
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
    """Fetch the list of vouchers from the Valence service."""
    req = urllib.request.Request(
        f"{SERVICE_URL}/api/vouchers",
        headers={"Authorization": f"Bearer {WEB_PASSCODE}"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())["vouchers"]


def fetch_voucher_xml(order_id: str) -> str:
    """Download the voucher XML for a given order."""
    req = urllib.request.Request(
        f"{SERVICE_URL}/review/orders/{order_id}/voucher",
        headers={"Authorization": f"Bearer {WEB_PASSCODE}"},
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
        return {"ok": False, "error": f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:200]}"}
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
        print(f"[sync] Found new voucher: {order_id} (customer={v['customer']}, total={v['total']})", flush=True)

        try:
            xml = fetch_voucher_xml(order_id)
        except Exception as exc:
            print(f"[error] Failed to download voucher XML for {order_id}: {exc}", flush=True)
            continue

        result = push_to_tally(xml)
        if result["ok"]:
            resp = result.get("response", "")
            if "LINEERROR" in resp:
                print(f"[error] Tally rejected {order_id}: {resp[:300]}", flush=True)
            else:
                print(f"[ok] Pushed {order_id} to Tally (status={result['status']})", flush=True)
                pushed.add(order_id)
                state["pushed"] = list(pushed)
                save_state(state)
        else:
            print(f"[error] Failed to push {order_id} to Tally: {result['error']}", flush=True)


def main():
    print(f"[tally_sync] Starting Tally sync", flush=True)
    print(f"  Service: {SERVICE_URL}", flush=True)
    print(f"  Tally:   {TALLY_URL}", flush=True)
    print(f"  Poll:    every {POLL_INTERVAL}s", flush=True)

    while True:
        poll()
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
