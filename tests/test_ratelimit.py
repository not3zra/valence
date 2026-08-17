"""The in-memory per-sender webhook rate limiter (security #32).

Pins the sliding-window contract: at most ``max_events`` hits per key per
window, old hits fall out, and rejected hits are not recorded (a key that hit
the cap needs only to wait out the window).
"""

from __future__ import annotations

from src.ratelimit import SlidingWindowRateLimiter


def test_allows_up_to_max_events_per_window():
    limiter = SlidingWindowRateLimiter(window_seconds=60.0, max_events=3)

    assert limiter.allow("+919812345001", now=0.0)
    assert limiter.allow("+919812345001", now=1.0)
    assert limiter.allow("+919812345001", now=2.0)
    assert not limiter.allow("+919812345001", now=3.0)


def test_keys_are_isolated_per_sender():
    limiter = SlidingWindowRateLimiter(window_seconds=60.0, max_events=1)

    assert limiter.allow("+919812345001", now=0.0)
    assert not limiter.allow("+919812345001", now=1.0)
    assert limiter.allow("+919845000001", now=1.0)


def test_window_slides_and_old_hits_fall_out():
    limiter = SlidingWindowRateLimiter(window_seconds=60.0, max_events=2)

    assert limiter.allow("sender", now=0.0)
    assert limiter.allow("sender", now=10.0)
    assert not limiter.allow("sender", now=59.0)
    # Past the window the earliest hit has fallen out, so one slot is free.
    assert limiter.allow("sender", now=61.0)
    assert not limiter.allow("sender", now=62.0)


def test_rejected_hits_are_not_recorded():
    limiter = SlidingWindowRateLimiter(window_seconds=60.0, max_events=1)

    assert limiter.allow("sender", now=0.0)
    assert not limiter.allow("sender", now=1.0)
    assert not limiter.allow("sender", now=2.0)
    # Once the single hit ages out, the key passes again.
    assert limiter.allow("sender", now=61.0)
