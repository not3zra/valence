"""External Tally communication: push a prepared voucher and audit the outcome (#66).

Voucher generation and status stay in :mod:`src.voucher` (issue #61); this module
owns only the outbound half. The Tally import endpoint and the per-instance URL
are injected at the boundary, so the flow can be exercised against a fake Tally
endpoint without a live on-prem install. A Tally ``LINEERROR`` or any non-success
response is a failure: it is surfaced to the caller and recorded on the order's
append-only event timeline, never silently swallowed.
"""

from __future__ import annotations

import asyncio
import html
import re
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .orders import EVENT_VOUCHER_PUSH_FAILED, EVENT_VOUCHER_PUSHED, OrderEvent
from .store import OrderStore
from .voucher import VoucherStore

TallyUrlProvider = Callable[[], Awaitable[str]]

_LINEERROR_RE = re.compile(
    r"<LINEERROR\b[^>]*>(.*?)</LINEERROR>", re.IGNORECASE | re.DOTALL
)


class TallyPushError(RuntimeError):
    """A push precondition failed or the instance has no Tally configuration."""


class TallyPushFailed(TallyPushError):
    """Tally rejected the voucher or the request to it failed."""


@dataclass(frozen=True)
class TallyPushResult:
    """The outcome of one push attempt."""

    ok: bool
    status: int | None = None
    response: str = ""
    error: str = ""
    line_errors: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "status": self.status,
            "error": self.error,
            "line_errors": list(self.line_errors),
        }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect must not forward the voucher (or a credential) off-host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


def _line_errors(body: str) -> tuple[str, ...]:
    return tuple(
        html.unescape(match.strip())
        for match in _LINEERROR_RE.findall(body)
        if match.strip()
    )


def _snippet(body: str, limit: int = 500) -> str:
    return body[:limit]


def push_voucher_to_tally(
    xml: str, url: str, *, timeout: float = 10.0
) -> TallyPushResult:
    """POST one voucher XML document to an instance's configured Tally endpoint.

    The URL comes from the instance's Tally Integration, never from a module
    constant. Redirects are refused and the body is bounded in the result so a
    hostile or broken endpoint cannot turn the push into an SSRF or flood the
    event timeline.
    """
    endpoint = (url or "").strip().rstrip("/")
    if not endpoint:
        return TallyPushResult(ok=False, error="Tally push URL is not configured")

    request = urllib.request.Request(
        endpoint,
        data=xml.encode("utf-8"),
        headers={"Content-Type": "application/xml"},
        method="POST",
    )
    try:
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return TallyPushResult(
            ok=False,
            status=exc.code,
            response=_snippet(body),
            error=f"HTTP {exc.code}: {_snippet(body, 200)}",
            line_errors=_line_errors(body),
        )
    except (OSError, urllib.error.URLError) as exc:
        return TallyPushResult(ok=False, error=str(exc))

    errors = _line_errors(body)
    if errors:
        return TallyPushResult(
            ok=False,
            status=status,
            response=_snippet(body),
            error="; ".join(errors),
            line_errors=errors,
        )
    return TallyPushResult(ok=True, status=status, response=_snippet(body))


class TallyPushService:
    """Push a prepared voucher and record the outcome on the order timeline."""

    def __init__(
        self, store: OrderStore, storage: VoucherStore, tally_url: TallyUrlProvider
    ) -> None:
        self._store = store
        self._storage = storage
        self._tally_url = tally_url

    async def push(self, order_id: str) -> TallyPushResult:
        order = await self._store.get_order(order_id)
        if order is None:
            raise TallyPushError(f"order {order_id} not found")
        if not order.voucher_id:
            raise TallyPushError(f"order {order_id} has no prepared voucher")
        xml = await self._storage.read(order.voucher_id)
        if xml is None:
            raise TallyPushError(f"voucher {order.voucher_id} is not available")

        url = (await self._tally_url()).strip()
        if not url:
            raise TallyPushError("Tally push URL is not configured")

        result = await asyncio.to_thread(push_voucher_to_tally, xml, url)
        await self._store.append_order_event(
            OrderEvent(
                order_id=order.order_id or order_id,
                event_type=(
                    EVENT_VOUCHER_PUSHED if result.ok else EVENT_VOUCHER_PUSH_FAILED
                ),
                payload={"voucher_id": order.voucher_id, **result.to_dict()},
            )
        )
        return result
