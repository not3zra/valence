"""External Tally voucher push: transport, per-instance config, audit trail (#66)."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.core import OrderProcessingCore
from src.orders import Order, OrderItem
from src.store import InMemoryOrderStore
from src.tally import (
    TallyPushError,
    TallyPushService,
    push_voucher_to_tally,
)
from src.voucher import InMemoryVoucherStore, prepare_voucher

CUSTOMER_PHONE = "+919812345001"

SUCCESS_XML = (
    "<ENVELOPE><BODY><IMPORTDATA><RESULT><TALLYMESSAGE>"
    "<VOUCHER>Imported</VOUCHER>"
    "</TALLYMESSAGE></RESULT></IMPORTDATA></BODY></ENVELOPE>"
)
LINEERROR_XML = (
    "<ENVELOPE><BODY><IMPORTDATA><RESULT><TALLYMESSAGE>"
    "<LINEERROR>Could not find Ledger 'Output CGST 9%'</LINEERROR>"
    "</TALLYMESSAGE></RESULT></IMPORTDATA></BODY></ENVELOPE>"
)
VOUCHER_XML = "<ENVELOPE><VOUCHER>fake</VOUCHER></ENVELOPE>"


class _FakeTallyHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        self.server.received.append(  # type: ignore[attr-defined]
            (self.path, self.headers.get("Content-Type"), self.rfile.read(length))
        )
        self.send_response(self.server.response_status)  # type: ignore[attr-defined]
        self.send_header("Content-Type", "application/xml")
        self.end_headers()
        self.wfile.write(self.server.response_body)  # type: ignore[attr-defined]

    def log_message(self, *args) -> None:
        return None


@pytest.fixture
def fake_tally():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeTallyHandler)
    server.received = []  # type: ignore[attr-defined]
    server.response_status = 200  # type: ignore[attr-defined]
    server.response_body = SUCCESS_XML.encode()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join()
    server.server_close()


def _url(server) -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}/import"


def test_push_posts_voucher_xml_to_the_configured_endpoint(fake_tally):
    result = push_voucher_to_tally(VOUCHER_XML, _url(fake_tally))

    assert result.ok is True
    assert result.status == 200
    assert fake_tally.received[0][1] == "application/xml"
    assert fake_tally.received[0][2].decode() == VOUCHER_XML


def test_push_surfaces_a_tally_line_error_as_failure(fake_tally):
    fake_tally.response_body = LINEERROR_XML.encode()

    result = push_voucher_to_tally(VOUCHER_XML, _url(fake_tally))

    assert result.ok is False
    assert "Could not find Ledger" in result.error
    assert result.line_errors == ("Could not find Ledger 'Output CGST 9%'",)


def test_push_surfaces_http_errors_and_network_failures(fake_tally):
    fake_tally.response_status = 500
    fake_tally.response_body = b"server exploded"
    http_result = push_voucher_to_tally(VOUCHER_XML, _url(fake_tally))
    assert http_result.ok is False
    assert "HTTP 500" in http_result.error

    offline = push_voucher_to_tally(VOUCHER_XML, "http://127.0.0.1:1/import")
    assert offline.ok is False
    assert offline.error

    unconfigured = push_voucher_to_tally(VOUCHER_XML, "  ")
    assert unconfigured.ok is False
    assert "not configured" in unconfigured.error.lower()


async def _prepared_order(store, storage, order_id="ord_push", phone=CUSTOMER_PHONE):
    decision = await OrderProcessingCore(store).process(
        Order(
            order_id=order_id,
            phone=phone,
            customer="ChemFab Industries",
            items=[OrderItem(product="sulfuric acid", quantity=2000, unit="kg")],
            delivery_location="Peenya Industrial Area",
            confidence=0.9,
        )
    )
    assert decision.approved, decision.escalation_reasons
    await prepare_voucher(store, storage, decision.order_id)
    return decision.order_id


def _service(store, storage, url: str) -> TallyPushService:
    async def tally_url() -> str:
        return url

    return TallyPushService(store, storage, tally_url)


async def test_push_service_uses_instance_url_and_records_success(fake_tally):
    store, storage = InMemoryOrderStore(), InMemoryVoucherStore()
    order_id = await _prepared_order(store, storage)

    result = await _service(store, storage, _url(fake_tally)).push(order_id)

    assert result.ok is True
    assert fake_tally.received[0][2].decode() == storage.blobs["voucher_" + order_id]
    events = await store.list_order_events(order_id)
    pushed = [event for event in events if event.event_type == "voucher_pushed"]
    assert len(pushed) == 1
    assert pushed[0].payload["voucher_id"] == "voucher_" + order_id
    assert pushed[0].payload["ok"] is True


async def test_push_service_records_failure_without_swallowing_the_error(fake_tally):
    fake_tally.response_body = LINEERROR_XML.encode()
    store, storage = InMemoryOrderStore(), InMemoryVoucherStore()
    order_id = await _prepared_order(store, storage)

    result = await _service(store, storage, _url(fake_tally)).push(order_id)

    assert result.ok is False
    events = await store.list_order_events(order_id)
    failed = [event for event in events if event.event_type == "voucher_push_failed"]
    assert len(failed) == 1
    assert "Could not find Ledger" in failed[0].payload["error"]
    assert failed[0].payload["line_errors"] == [
        "Could not find Ledger 'Output CGST 9%'"
    ]


async def test_push_service_requires_a_prepared_voucher_and_a_configured_url():
    store, storage = InMemoryOrderStore(), InMemoryVoucherStore()

    def blank_service(target_store, target_storage) -> TallyPushService:
        async def blank_url() -> str:
            return ""

        return TallyPushService(target_store, target_storage, blank_url)

    service = blank_service(store, storage)
    with pytest.raises(TallyPushError, match="not found"):
        await service.push("ord_missing")

    decision = await OrderProcessingCore(store).process(
        Order(
            order_id="ord_unprepared",
            phone=CUSTOMER_PHONE,
            customer="ChemFab Industries",
            items=[OrderItem(product="sulfuric acid", quantity=2000, unit="kg")],
            delivery_location="Peenya Industrial Area",
            confidence=0.9,
        )
    )
    with pytest.raises(TallyPushError, match="no prepared voucher"):
        await service.push(decision.order_id)

    fresh_store, fresh_storage = InMemoryOrderStore(), InMemoryVoucherStore()
    order_id = await _prepared_order(fresh_store, fresh_storage)
    with pytest.raises(TallyPushError, match="not configured"):
        await blank_service(fresh_store, fresh_storage).push(order_id)
    events = await fresh_store.list_order_events(order_id)
    assert not any(
        event.event_type in ("voucher_pushed", "voucher_push_failed")
        for event in events
    )
