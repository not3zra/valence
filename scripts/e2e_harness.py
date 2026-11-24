#!/usr/bin/env python3
"""One-command, real-process end-to-end harness for Portable Valence v1 (#93).

Starts a clean SQLite Instance through the ``valence`` CLI, drives it through
real HTTP, pushes to a local fake Tally, delivers a signed Meta webhook,
restarts the application process, and verifies persistence. Every step is
reported and all logs and artifacts are kept for diagnosis.

    python scripts/e2e_harness.py [--artifacts DIR]
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "examples" / "acme-chemicals"
APP_SECRET = "e2e-app-secret"
VERIFY_TOKEN = "e2e-verify-token"
SENDER = "+919812345001"
WEBHOOK_SENDER = "919812345002"
APPROVER_PHONE = "+919845000001"
HEALTH_TIMEOUT_S = 45
SUCCESS_XML = (
    b"<ENVELOPE><BODY><RESULT><TALLYMESSAGE><VOUCHER>ok</VOUCHER>"
    b"</TALLYMESSAGE></RESULT></BODY></ENVELOPE>"
)


class HarnessError(RuntimeError):
    """A harness step failed with a diagnosable message."""


class _TallyHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.server.received.append(  # type: ignore[attr-defined]
            {
                "path": self.path,
                "content_type": self.headers.get("Content-Type"),
                "body": body.decode("utf-8", "replace"),
            }
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.end_headers()
        self.wfile.write(SUCCESS_XML)

    def log_message(self, *args: Any) -> None:
        return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_tally() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TallyHandler)
    server.received = []  # type: ignore[attr-defined]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _tally_url(server: ThreadingHTTPServer) -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}/import"


class HttpClient:
    """Minimal stdlib HTTP client that turns non-expected statuses into errors."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        body: dict[str, Any] | bytes | None = None,
        headers: dict[str, str] | None = None,
        expected: int = 200,
    ) -> tuple[int, bytes]:
        request_headers = dict(headers or {})
        data: bytes | None = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            request_headers.setdefault("Content-Type", "application/json")
        if token is not None:
            request_headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=request_headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                status, payload = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, payload = error.code, error.read()
        if status != expected:
            raise HarnessError(
                f"{method} {path} returned {status}, expected {expected}: "
                f"{payload[:400]!r}"
            )
        return status, payload

    def json(
        self, method: str, path: str, **kwargs: Any
    ) -> dict[str, Any] | list[dict[str, Any]]:
        return json.loads(self.request(method, path, **kwargs)[1])


def _run_cli(env: dict[str, str], *args: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "src.cli", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=90,
    )
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-8:])
        raise HarnessError(
            f"valence {' '.join(args)} exited {result.returncode}:\n{tail}"
        )
    return result.stdout


def _start_app(
    env: dict[str, str], port: int, log_path: Path
) -> subprocess.Popen[bytes]:
    log = log_path.open("ab")
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "src.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
        ],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def _stop_app(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            process.kill()
        process.wait(timeout=10)


def _boot_app(
    env: dict[str, str], port: int, log_path: Path
) -> tuple[subprocess.Popen[bytes], HttpClient]:
    process = _start_app(env, port, log_path)
    client = HttpClient(f"http://127.0.0.1:{port}")
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            _stop_app(process)
            raise HarnessError(
                f"application process exited during boot:\n{_log_tail(log_path)}"
            )
        try:
            if client.request("GET", "/health")[0] == 200:
                return process, client
        except (urllib.error.URLError, OSError, HarnessError):
            time.sleep(0.2)
    _stop_app(process)
    raise HarnessError(f"application did not become healthy:\n{_log_tail(log_path)}")


def _log_tail(path: Path, lines: int = 40) -> str:
    if not path.exists():
        return "(no server log)"
    content = path.read_text(errors="replace").splitlines()
    return "\n".join(content[-lines:])


def _whatsapp_payload(message: str, sender: str) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "E2E_WHATSAPP_BUSINESS_ACCOUNT",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "16505551111",
                                "phone_number_id": "123456789012345",
                            },
                            "messages": [
                                {
                                    "from": sender,
                                    "id": "wamid.e2e",
                                    "timestamp": "1700000000",
                                    "type": "text",
                                    "text": {"body": message},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def _contains_order(render: dict[str, Any], order_id: str) -> bool:
    section_entries = [
        entry for section in render["sections"] for entry in section["entries"]
    ]
    entries = [*section_entries, *render["late"], *render["unrouted"]]
    return any(entry["order_id"] == order_id for entry in entries)


def _business_day(order: dict[str, Any]) -> str:
    moment = datetime.fromisoformat(order["updated_at"])
    return moment.astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat()


def _act(
    client: HttpClient, approver_key: str, order_id: str, action: str
) -> dict[str, Any]:
    result = client.json(
        "POST",
        f"/api/v1/orders/{order_id}/actions",
        token=approver_key,
        body={"action": action, "confirm": True},
    )
    assert isinstance(result, dict), result
    return result


def run(artifacts: Path, database_url: str | None = None) -> dict[str, Any]:
    artifacts.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "status": "running",
        "steps": [],
        "artifacts": str(artifacts),
    }
    steps: list[dict[str, str]] = report["steps"]

    def write_report() -> None:
        (artifacts / "report.json").write_text(json.dumps(report, indent=2) + "\n")

    @contextmanager
    def step(name: str) -> Iterator[None]:
        try:
            yield
        except Exception as error:
            steps.append({"name": name, "status": "fail", "error": str(error)})
            report["status"] = "fail"
            write_report()
            print(f"FAIL {name}: {error}", file=sys.stderr, flush=True)
            raise
        steps.append({"name": name, "status": "pass"})
        print(f"PASS {name}", flush=True)
        write_report()

    database_url = database_url or f"sqlite+aiosqlite:///{artifacts / 'valence.db'}"
    session_service = "postgres" if database_url.startswith("postgresql") else "sqlite"
    env = {
        **os.environ,
        "ORDER_STORE": session_service,
        "SESSION_SERVICE": session_service,
        "DATABASE_URL": database_url,
        "VOUCHER_DIR": str(artifacts / "vouchers"),
        "MODEL_PROVIDER": "fake-toolcall",
        "WEB_COOKIE_SECURE": "0",
        "TALLY_PUSH_URL": "",
        "META_APP_SECRET": "",
        "META_ACCESS_TOKEN": "",
        "META_PHONE_NUMBER_ID": "",
    }
    port = _free_port()
    log_path = artifacts / "server.log"
    tally = _start_tally()
    process: subprocess.Popen[bytes] | None = None
    try:
        with step("init"):
            output = _run_cli(
                env,
                "init",
                "--json",
                "--database-url",
                database_url,
                "--business-name",
                "Acme Chemicals",
                "--timezone",
                "Asia/Kolkata",
            )
            issued_admin = json.loads(output.strip().splitlines()[-1])
            admin_key = issued_admin["token"]
            (artifacts / "init.json").write_text(
                json.dumps({**issued_admin, "token": "<redacted>"}, indent=2)
            )

        with step("boot"):
            process, client = _boot_app(env, port, log_path)
        try:
            with step("setup"):
                output = _run_cli(
                    env,
                    "setup",
                    "--url",
                    f"http://127.0.0.1:{port}",
                    "--admin-key",
                    admin_key,
                    "--fixture",
                    str(FIXTURE),
                )
                (artifacts / "setup.log").write_text(output)

            with step("configure integrations"):
                client.json(
                    "PUT",
                    "/api/v1/integrations/tally",
                    token=admin_key,
                    body={"url": _tally_url(tally)},
                )
                client.json(
                    "PUT",
                    "/api/v1/integrations/meta",
                    token=admin_key,
                    body={"app_secret": APP_SECRET, "verify_token": VERIFY_TOKEN},
                )

            with step("issue approver key"):
                issued = client.json(
                    "POST",
                    "/api/v1/keys",
                    token=admin_key,
                    body={
                        "name": "e2e-approver",
                        "role": "approver",
                        "phone": APPROVER_PHONE,
                    },
                    expected=201,
                )
                assert isinstance(issued, dict)
                approver_key = issued["token"]

            with step("intake"):
                intake = client.json(
                    "POST",
                    "/api/v1/intake",
                    token=admin_key,
                    body={
                        "sender_id": SENDER,
                        "message": "2000 kg sulfuric acid to Peenya",
                    },
                    headers={"Idempotency-Key": "e2e-intake-1"},
                )
                assert isinstance(intake, dict)
                order_id = intake["order_id"]
                assert order_id, intake
                (artifacts / "intake.json").write_text(json.dumps(intake, indent=2))

            with step("approve"):
                result = _act(client, approver_key, order_id, "approve")
                assert result["status"] == "approved"

            with step("prepare voucher"):
                _act(client, approver_key, order_id, "prepare_voucher")
                _, xml = client.request(
                    "GET", f"/api/v1/orders/{order_id}/voucher", token=approver_key
                )
                assert xml.startswith(b"<")
                (artifacts / "voucher.xml").write_bytes(xml)

            with step("loading lists"):
                detail = client.json(
                    "GET", f"/api/v1/orders/{order_id}", token=admin_key
                )
                assert isinstance(detail, dict)
                day = _business_day(detail)
                render = client.json(
                    "GET", f"/api/v1/loading-lists?day={day}", token=admin_key
                )
                assert isinstance(render, dict) and _contains_order(render, order_id)
                cutoff = client.json(
                    "POST",
                    "/api/v1/loading-lists",
                    token=admin_key,
                    body={"day": day},
                )
                assert isinstance(cutoff, dict) and cutoff["delivery_day"] == day
                (artifacts / "loading-list.json").write_text(
                    json.dumps(render, indent=2)
                )

            with step("bill"):
                result = _act(client, approver_key, order_id, "bill")
                assert result["status"] == "billed"

            with step("push to tally"):
                _act(client, approver_key, order_id, "push_to_tally")
                assert len(tally.received) == 1, tally.received  # type: ignore[attr-defined]
                pushed = tally.received[0]  # type: ignore[attr-defined]
                assert pushed["content_type"].startswith("application/xml")
                assert "<VOUCHER" in pushed["body"]
                (artifacts / "tally-push.json").write_text(
                    json.dumps(tally.received, indent=2, default=str)  # type: ignore[attr-defined]
                )

            with step("whatsapp handshake"):
                query = urllib.parse.urlencode(
                    {
                        "hub.mode": "subscribe",
                        "hub.verify_token": VERIFY_TOKEN,
                        "hub.challenge": "e2e-challenge",
                    }
                )
                _, challenge = client.request(
                    "GET", f"/api/v1/webhooks/whatsapp?{query}", expected=200
                )
                assert challenge == b"e2e-challenge"

            with step("whatsapp webhook"):
                raw = json.dumps(
                    _whatsapp_payload("100 kg hydrochloric acid", WEBHOOK_SENDER)
                ).encode()
                signature = (
                    "sha256="
                    + hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
                )
                client.request(
                    "POST",
                    "/api/v1/webhooks/whatsapp",
                    body=raw,
                    headers={"X-Hub-Signature-256": signature},
                )
                webhook_orders = client.json(
                    "GET", f"/api/v1/orders?phone=%2B{WEBHOOK_SENDER}", token=admin_key
                )
                assert isinstance(webhook_orders, list) and webhook_orders
                webhook_id = webhook_orders[0]["order_id"]
                _act(client, approver_key, webhook_id, "approve")
                webhook_detail = client.json(
                    "GET", f"/api/v1/orders/{webhook_id}", token=admin_key
                )
                assert isinstance(webhook_detail, dict)
                webhook_day = _business_day(webhook_detail)
                (artifacts / "webhook-order.json").write_text(
                    json.dumps(webhook_detail, indent=2)
                )
        finally:
            with step("stop app"):
                _stop_app(process)

        with step("restart"):
            process, client = _boot_app(env, port, log_path)
        try:
            with step("persistence"):
                detail = client.json(
                    "GET", f"/api/v1/orders/{order_id}", token=approver_key
                )
                assert isinstance(detail, dict)
                assert detail["status"] == "billed" and detail["voucher_id"]
                _, resumed_xml = client.request(
                    "GET", f"/api/v1/orders/{order_id}/voucher", token=approver_key
                )
                assert resumed_xml == xml
                resumed = client.json(
                    "GET", f"/api/v1/loading-lists?day={webhook_day}", token=admin_key
                )
                assert isinstance(resumed, dict) and _contains_order(
                    resumed, webhook_id
                )
                events = detail["events"]
                assert "voucher_pushed" in {event["event_type"] for event in events}
        finally:
            with step("stop app after restart"):
                _stop_app(process)
    finally:
        tally.shutdown()
        tally.server_close()

    report["status"] = "pass"
    write_report()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifacts",
        default=None,
        help="directory for logs and artifacts (default: a fresh temp directory)",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="SQLAlchemy async URL (default: a clean SQLite database in --artifacts; "
        "a postgresql+asyncpg:// URL switches both store and sessions to Postgres)",
    )
    args = parser.parse_args(argv)
    artifacts = (
        Path(args.artifacts)
        if args.artifacts
        else Path(tempfile.mkdtemp(prefix="valence-e2e-"))
    )
    try:
        run(artifacts, args.database_url)
    except Exception as error:
        print(
            f"E2E FAIL: {error}\nArtifacts preserved at: {artifacts}", file=sys.stderr
        )
        return 1
    print(f"E2E PASS — artifacts: {artifacts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
