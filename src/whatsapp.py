"""The WhatsApp channel boundary: the neutral inbound message shape and the
outbound sender seam.

Meta Cloud API is a boundary adapter behind these seams (issue #4 design note):
the shared handler only ever sees ``InboundMessage``, a ``WhatsAppSender``, and
a ``WhatsAppWebhookParser``. ``MockWhatsAppSender`` is the demo/test wiring that
records/intercepts the reply; the live sender is ``MetaWhatsAppSender`` (issue
#13), wired by the deployment entry point.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class InboundMessage:
    """A provider-neutral inbound WhatsApp message.

    ``sender`` is the E.164 phone number of the sender (a ``whatsapp:`` prefix
    or a missing ``+``, as a provider sends it, is already normalized by the
    provider adapter). ``media`` is a provider-free list of media references —
    URLs for a URL-fetching provider, ids for Meta's media-id model — fetched
    by the ``MediaFetcher`` seam (issue #11 / #13).
    """

    sender: str
    body: str
    media: tuple[str, ...] = ()


class WhatsAppSender(Protocol):
    """Outbound seam: deliver ``text`` to ``recipient`` (E.164)."""

    def send(self, recipient: str, text: str) -> None: ...


class WhatsAppWebhookParser(Protocol):
    """Inbound seam: own a provider's webhook handshake, signature verification,
    and payload parsing, returning provider-neutral data.

    The routing layer never touches the provider — it only calls these three
    methods. ``verification_challenge`` returns the value to echo when the
    request is a provider verification handshake (Meta's ``hub.challenge``
    GET), or ``None`` when it is not (or does not verify); ``verify_signature``
    checks the provider's request signature; ``parse`` turns the provider's raw
    request body into neutral ``InboundMessage`` objects — a list, since a
    provider may deliver several messages in one webhook POST — empty when the
    request carried no message to process.
    """

    def verification_challenge(
        self, *, method: str, query: dict[str, str]
    ) -> str | None: ...
    def verify_signature(
        self, *, method: str, url: str, headers: dict[str, str], body: bytes
    ) -> bool: ...
    def parse(self, *, method: str, body: bytes) -> list[InboundMessage]: ...


class MockWhatsAppSender:
    """Demo sender that records messages instead of delivering them."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, recipient: str, text: str) -> None:
        self.sent.append((recipient, text))
