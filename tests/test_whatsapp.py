"""WhatsApp channel boundary: inbound parsing, signature verification, and the
outbound sender seam (issue #4).

Twilio is a boundary adapter behind a seam (per the channel-adapter design note
on issue #4): the parser turns Twilio's form-encoded ``Body``/``From`` shape
into a provider-neutral ``InboundMessage``, signature verification is the
shared ``src.twilio`` algorithm (the same HMAC-SHA1 the Voice callback uses),
and the outbound side is a ``WhatsAppSender`` interface whose demo wiring is
``MockWhatsAppSender`` — a real provider sender is a later swap.
"""

from __future__ import annotations

from src.twilio import build_twilio_signature, verify_twilio_signature
from src.twilio_whatsapp import TwilioWhatsAppParser
from src.whatsapp import MockWhatsAppSender

# --- Inbound parsing ---------------------------------------------------------


def test_parser_normalizes_twilio_form_to_neutral_message():
    form = {
        "From": "whatsapp:+919812345001",
        "Body": "Namaste, 2 drums sulfuric acid chahiye",
        "NumMedia": "0",
    }
    message = TwilioWhatsAppParser().parse(form)
    assert message is not None
    assert message.sender == "+919812345001"
    assert message.body == "Namaste, 2 drums sulfuric acid chahiye"
    assert message.media == ()


def test_parser_strips_whatsapp_prefix_from_sender():
    message = TwilioWhatsAppParser().parse(
        {"From": "whatsapp:+919812345002", "Body": "x"}
    )
    assert message.sender == "+919812345002"


def test_parser_keeps_media_provider_free():
    # Twilio's MediaUrlN shape maps to a neutral media list; nothing about the
    # provider's field names leaks into the shared message shape.
    message = TwilioWhatsAppParser().parse(
        {
            "From": "whatsapp:+919812345001",
            "Body": "see photo",
            "NumMedia": "2",
            "MediaUrl0": "https://example.com/a.jpg",
            "MediaUrl1": "https://example.com/b.jpg",
        }
    )
    assert message is not None
    assert message.media == (
        "https://example.com/a.jpg",
        "https://example.com/b.jpg",
    )


def test_parser_returns_none_without_sender():
    assert TwilioWhatsAppParser().parse({"Body": "hello"}) is None


# --- Signature verification --------------------------------------------------


def test_signature_matches_twilio_documented_example():
    # Twilio's own test vector (twilio-java/php RequestValidatorTest): URL
    # https://mycompany.com/myapp.php?foo=1&bar=2, params CallSid/To/Caller/
    # From/Digits, auth token 12345 -> RSOYDt4T1cUTdK1PDd93/VVr8B8=
    params = {
        "Digits": "1234",
        "CallSid": "CA1234567890ABCDE",
        "To": "+18005551212",
        "Caller": "+14158675309",
        "From": "+14158675309",
    }
    signature = build_twilio_signature(
        "https://mycompany.com/myapp.php?foo=1&bar=2", params, "12345"
    )
    assert signature == "RSOYDt4T1cUTdK1PDd93/VVr8B8="
    assert verify_twilio_signature(
        "https://mycompany.com/myapp.php?foo=1&bar=2", params, signature, "12345"
    )


def test_signature_rejects_wrong_auth_token():
    params = {"From": "whatsapp:+919812345001", "Body": "x"}
    signature = build_twilio_signature("http://host/webhook", params, "token-a")
    assert not verify_twilio_signature(
        "http://host/webhook", params, signature, "token-b"
    )


def test_signature_rejects_tampered_params():
    params = {"From": "whatsapp:+919812345001", "Body": "x"}
    signature = build_twilio_signature("http://host/webhook", params, "12345")
    tampered = dict(params)
    tampered["Body"] = "y"
    assert not verify_twilio_signature(
        "http://host/webhook", tampered, signature, "12345"
    )


def test_signature_rejects_missing_header():
    params = {"From": "whatsapp:+919812345001", "Body": "x"}
    assert not verify_twilio_signature("http://host/webhook", params, "", "12345")


def test_signature_rejects_empty_auth_token():
    params = {"From": "whatsapp:+919812345001", "Body": "x"}
    signature = build_twilio_signature("http://host/webhook", params, "")
    assert not verify_twilio_signature("http://host/webhook", params, signature, "")


# --- Outbound sender seam ----------------------------------------------------


def test_mock_whatsapp_sender_records_outbound_messages():
    sender = MockWhatsAppSender()
    sender.send("+919812345001", "Order confirmed")
    sender.send("+919812345002", "Under approval")
    assert sender.sent == [
        ("+919812345001", "Order confirmed"),
        ("+919812345002", "Under approval"),
    ]
