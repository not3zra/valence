"""The WhatsApp channel boundary: the neutral inbound message shape and the
outbound sender seam.

Twilio (and later Meta Cloud API) are boundary adapters behind these seams
(issue #4 design note): the shared handler only ever sees ``InboundMessage``
and a ``WhatsAppSender``. ``MockWhatsAppSender`` is the demo wiring that
records/intercepts the reply; a real provider sender is a later swap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class InboundMessage:
    """A provider-neutral inbound WhatsApp message.

    ``sender`` is the E.164 phone number of the sender (the ``whatsapp:``
    prefix, if any, is already stripped by the provider adapter). ``media`` is
    a provider-free list of media references (URLs or ids) — photo intake is
    ticket 11, so the shape is neutral even though Twilio's form uses
    ``MediaUrlN``.
    """

    sender: str
    body: str
    media: tuple[str, ...] = ()


class WhatsAppSender(Protocol):
    """Outbound seam: deliver ``text`` to ``recipient`` (E.164)."""

    def send(self, recipient: str, text: str) -> None: ...


class WhatsAppWebhookParser(Protocol):
    """Inbound seam: turn a provider's raw webhook payload into an InboundMessage.

    ``form`` is the provider's request fields as name/value strings; the
    concrete parser (Twilio today, Meta Cloud API as a later swap) owns that
    shape. Returning ``None`` means the request carried no message to process.
    """

    def parse(self, form: dict[str, str]) -> InboundMessage | None: ...


class MockWhatsAppSender:
    """Demo sender that records messages instead of delivering them."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, recipient: str, text: str) -> None:
        self.sent.append((recipient, text))
