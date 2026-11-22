"""Fake Tally import endpoint shared by the push tests (issue #66)."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

SUCCESS_XML = (
    "<ENVELOPE><BODY><RESULT><TALLYMESSAGE><VOUCHER>ok</VOUCHER>"
    "</TALLYMESSAGE></RESULT></BODY></ENVELOPE>"
)
LINEERROR_XML = (
    "<ENVELOPE><BODY><RESULT><TALLYMESSAGE>"
    "<LINEERROR>Could not find Ledger 'Output CGST 9%'</LINEERROR>"
    "</TALLYMESSAGE></RESULT></BODY></ENVELOPE>"
)


class FakeTallyHandler(BaseHTTPRequestHandler):
    """Records each POSTed voucher and replies with a configurable response."""

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
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeTallyHandler)
    server.received = []  # type: ignore[attr-defined]
    server.response_status = 200  # type: ignore[attr-defined]
    server.response_body = SUCCESS_XML.encode()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join()
    server.server_close()


def tally_url(server) -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}/import"
