"""Twilio WhatsApp adapter: form parsing and ``X-Twilio-Signature`` verification.

Everything Twilio-specific lives here, behind the seam defined in
``src.whatsapp`` (issue #4 design note): parsing turns Twilio's form-encoded
``Body``/``From`` shape into a provider-neutral ``InboundMessage``, and
signature verification is shared with the Voice callback in ``src.twilio``
(one HMAC-SHA1 algorithm for every Twilio webhook) so the webhook routing layer
stays provider-free. Swapping to Meta Cloud API later is a contained change to
this module, not a rewrite.
"""

from __future__ import annotations

from .whatsapp import InboundMessage

# Twilio's WhatsApp sandbox sends the sender as "whatsapp:+<E.164>".
WHATSAPP_PREFIX = "whatsapp:"


class TwilioWhatsAppParser:
    """Parse a Twilio WhatsApp webhook's form fields into an ``InboundMessage``.

    Twilio POSTs ``application/x-www-form-urlencoded`` with ``From`` (the
    sender, prefixed ``whatsapp:``), ``Body``, and ``NumMedia``/``MediaUrlN``
    when a photo is attached. Media stays provider-free: ``MediaUrlN`` becomes
    a plain tuple of URLs.
    """

    def parse(self, form: dict[str, str]) -> InboundMessage | None:
        from_ = (form.get("From") or "").strip()
        if not from_:
            return None
        sender = from_
        if sender.startswith(WHATSAPP_PREFIX):
            sender = sender[len(WHATSAPP_PREFIX) :]

        media: list[str] = []
        try:
            num_media = int(form.get("NumMedia") or 0)
        except ValueError:
            num_media = 0
        for index in range(num_media):
            url = (form.get(f"MediaUrl{index}") or "").strip()
            if url:
                media.append(url)

        return InboundMessage(
            sender=sender,
            body=(form.get("Body") or ""),
            media=tuple(media),
        )
