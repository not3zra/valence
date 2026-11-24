"""The local Tally sync script uses the v1 Agent Interface (issue #99)."""

from __future__ import annotations

import json

from scripts import tally_sync


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _capture(monkeypatch, body: bytes):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, request.get_header("Authorization"), timeout))
        return _FakeResponse(body)

    monkeypatch.setattr(tally_sync.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(tally_sync, "SERVICE_URL", "http://valence:8080")
    monkeypatch.setattr(tally_sync, "API_KEY", "approver-key")
    return calls


def test_fetch_vouchers_lists_orders_with_vouchers_through_v1(monkeypatch):
    calls = _capture(
        monkeypatch,
        json.dumps(
            [
                {
                    "order_id": "ord_1",
                    "voucher_id": "voucher_ord_1",
                    "customer": "ChemFab",
                    "draft_value_inr": 4200.0,
                    "status": "billed",
                },
                {
                    "order_id": "ord_2",
                    "voucher_id": None,
                    "customer": "Maruthi",
                    "draft_value_inr": 100.0,
                    "status": "approved",
                },
            ]
        ).encode(),
    )

    vouchers = tally_sync.fetch_vouchers()

    assert calls == [("http://valence:8080/api/v1/orders", "Bearer approver-key", 15)]
    assert vouchers == [
        {
            "order_id": "ord_1",
            "voucher_id": "voucher_ord_1",
            "customer": "ChemFab",
            "total": 4200.0,
            "status": "billed",
        }
    ]


def test_fetch_voucher_xml_downloads_through_v1(monkeypatch):
    calls = _capture(monkeypatch, b"<ENVELOPE/>")

    xml = tally_sync.fetch_voucher_xml("ord_1")

    assert calls == [
        (
            "http://valence:8080/api/v1/orders/ord_1/voucher",
            "Bearer approver-key",
            15,
        )
    ]
    assert xml == "<ENVELOPE/>"
