"""Order domain: the linear status state machine and the two-tier draft money.

These are pure functions with no adapters, so every branch is exercised
directly. They pin the domain rules the decision engine composes: the linear
``pending_review -> approved -> dispatched -> billed`` chain, the ``rejected``
terminal status, and the agreed-rate > tier > catalog price precedence.
"""

from __future__ import annotations

import pytest

from src.money import estimate_rate, quantity_is_anomalous, rate_is_anomalous
from src.orders import OrderStatus, is_terminal, transition


def test_linear_chain_from_pending_review_to_billed():
    status = OrderStatus.PENDING_REVIEW
    next_statuses = (
        OrderStatus.APPROVED,
        OrderStatus.DISPATCHED,
        OrderStatus.BILLED,
    )
    for next_status in next_statuses:
        status = transition(status, next_status)
    assert status is OrderStatus.BILLED


def test_approved_can_skip_directly_to_billed():
    assert transition(OrderStatus.APPROVED, OrderStatus.BILLED) is OrderStatus.BILLED


def test_pending_review_can_be_rejected():
    result = transition(OrderStatus.PENDING_REVIEW, OrderStatus.REJECTED)
    assert result is OrderStatus.REJECTED


def test_illegal_transitions_raise():
    with pytest.raises(ValueError):
        transition(OrderStatus.PENDING_REVIEW, OrderStatus.DISPATCHED)
    with pytest.raises(ValueError):
        transition(OrderStatus.PENDING_REVIEW, OrderStatus.BILLED)
    with pytest.raises(ValueError):
        transition(OrderStatus.BILLED, OrderStatus.APPROVED)
    with pytest.raises(ValueError):
        transition(OrderStatus.REJECTED, OrderStatus.APPROVED)


def test_rejected_and_billed_are_terminal():
    assert is_terminal(OrderStatus.BILLED)
    assert is_terminal(OrderStatus.REJECTED)
    assert not is_terminal(OrderStatus.PENDING_REVIEW)
    assert not is_terminal(OrderStatus.APPROVED)


def test_agreed_rate_beats_tier_beats_catalog_price():
    assert estimate_rate(17.5, 18.0, 18.5) == 17.5
    assert estimate_rate(None, 18.0, 18.5) == 18.0
    assert estimate_rate(None, None, 18.5) == 18.5


def test_quantity_anomaly_is_strictly_above_the_deviation():
    assert not quantity_is_anomalous(6000, 4000, 0.5)  # exactly 50% above -> ok
    assert quantity_is_anomalous(6001, 4000, 0.5)
    assert not quantity_is_anomalous(7000, None, 0.5)  # no history -> never anomalous


def test_rate_anomaly_uses_percent_off_the_agreed_rate():
    assert not rate_is_anomalous(21.0, 17.5, 0.2)  # exactly 20% off -> ok
    assert rate_is_anomalous(22.0, 17.5, 0.2)  # ~26% above -> anomalous
    assert rate_is_anomalous(12.0, 17.5, 0.2)  # ~31% below -> anomalous
    assert not rate_is_anomalous(17.5, None, 0.2)  # no agreed rate -> never anomalous
    assert not rate_is_anomalous(None, 17.5, 0.2)  # no stated rate -> never anomalous
