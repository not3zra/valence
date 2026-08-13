"""Two-tier draft money model (ADR-0002).

Draft estimates — the only money ever computed at intake — follow the
precedence agreed rate > pricing tier > catalog current price. They drive the
value cap, the review display, and anomaly detection only; the authoritative
amounts are locked at Tally voucher generation, never here.
"""

from __future__ import annotations


def estimate_rate(
    agreed_rate: float | None,
    tier_rate: float | None,
    current_price: float,
) -> float:
    """Resolve a line's draft rate: agreed rate beats tier beats catalog."""
    if agreed_rate is not None:
        return agreed_rate
    if tier_rate is not None:
        return tier_rate
    return current_price


def quantity_is_anomalous(
    quantity: float,
    max_quantity: float | None,
    deviation_above_pct: float,
) -> bool:
    """True when a line exceeds the customer's 90-day max by the configured %.

    ``max_quantity`` of ``None`` (no history for the product) is never an
    anomaly.
    """
    if max_quantity is None:
        return False
    return quantity > max_quantity * (1 + deviation_above_pct)


def rate_is_anomalous(
    stated_rate: float | None,
    agreed_rate: float | None,
    deviation_pct: float,
) -> bool:
    """True when a stated rate is more than the configured % off the agreed rate.

    A missing stated rate or a product with no agreed rate is never an anomaly.
    """
    if stated_rate is None or agreed_rate is None or agreed_rate == 0:
        return False
    return abs(stated_rate - agreed_rate) / agreed_rate > deviation_pct
