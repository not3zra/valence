"""In-memory per-key sliding-window rate limiter (security #32).

The WhatsApp webhook is signature-gated (Meta proves the sender is real) but
not quota-gated: any real or compromised number can flood the webhook and burn
a Gemini turn per message. The limiter caps turns per sender per window.

Per-instance by design: the demo deploys a single Cloud Run instance, where the
window is exact. Across multiple instances the limit is per-instance, not
global — a shared store would be needed to make it exact — which THREAT_MODEL.md
records as a carried-over judgment call.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque


class SlidingWindowRateLimiter:
    """Allow at most ``max_events`` hits per key inside ``window_seconds``.

    Rejected (over-limit) hits are not recorded, so a key that hit the cap only
    needs to wait out the window before it can pass again.
    """

    def __init__(self, window_seconds: float = 60.0, max_events: int = 30) -> None:
        self.window_seconds = window_seconds
        self.max_events = max_events
        self._hits: defaultdict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        window_start = now - self.window_seconds
        hits = self._hits[key]
        while hits and hits[0] < window_start:
            hits.popleft()
        if len(hits) >= self.max_events:
            return False
        hits.append(now)
        return True
