"""The dispatch-channel WhatsApp heads-up for late orders (issue #9).

When an order is approved after the daily cutoff, the dispatch channel is
notified instantly over WhatsApp and the event is recorded on the audit trail.
"""

from __future__ import annotations

import pytest

from src.dispatch import LateOrderNotifier
from src.orders import EVENT_ORDER_LATE
from src.store import InMemoryOrderStore
from src.whatsapp import MockWhatsAppSender

DISPATCH_NUMBER = "+919845000003"


@pytest.fixture
def store():
    return InMemoryOrderStore()


@pytest.fixture
def sender():
    return MockWhatsAppSender()


async def test_late_notifier_sends_to_dispatch_channel(store, sender):
    notifier = LateOrderNotifier(store, sender)
    await notifier.on_order_late("ord_late_1")

    assert len(sender.sent) == 1
    recipient, text = sender.sent[0]
    assert recipient == DISPATCH_NUMBER
    assert "ord_late_1" in text
    assert "Late order" in text


async def test_late_notifier_records_order_late_event(store, sender):
    notifier = LateOrderNotifier(store, sender)
    await notifier.on_order_late("ord_late_2")

    event = store.events[-1]
    assert event.event_type == EVENT_ORDER_LATE
    assert event.order_id == "ord_late_2"
    assert event.payload["channel"] == DISPATCH_NUMBER


async def test_late_notifier_does_nothing_when_channel_unconfigured(store, sender):
    # InMemoryOrderStore with no config (or config without dispatch_whatsapp_number)
    store = InMemoryOrderStore(config={})
    notifier = LateOrderNotifier(store, sender)
    await notifier.on_order_late("ord_late_3")

    assert sender.sent == []
    assert store.events == []


async def test_late_notifier_uses_config_channel(store, sender):
    # Explicitly set a different channel in config
    store = InMemoryOrderStore(config={"dispatch_whatsapp_number": "+919999999999"})
    notifier = LateOrderNotifier(store, sender)
    await notifier.on_order_late("ord_late_4")

    recipient, _ = sender.sent[0]
    assert recipient == "+919999999999"
