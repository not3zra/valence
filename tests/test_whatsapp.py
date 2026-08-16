"""WhatsApp channel boundary: inbound parsing, signature verification, and the
outbound sender seam (issue #4, then the Meta Cloud API swap, issue #13).

The Meta adapter is a boundary adapter behind the seam defined in ``src.whatsapp``:
``MetaWhatsAppParser`` turns Meta's nested JSON webhook (``entry``/``changes``/
``value``/``messages``) into a provider-neutral ``InboundMessage`` and owns the
provider's verification — the GET handshake and the ``X-Hub-Signature-256``
HMAC-SHA256 of the raw body with the App Secret. ``MetaWhatsAppSender`` POSTs
replies to the Graph API ``/messages`` endpoint. The shared Twilio signature
algorithm (``src.twilio``) is kept: Twilio Voice webhooks still use it.
"""

from __future__ import annotations

import json

from src.meta_whatsapp import (
    MetaWhatsAppParser,
    MetaWhatsAppSender,
    build_meta_signature,
    verify_meta_signature,
)
from src.twilio import build_twilio_signature, verify_twilio_signature
from src.whatsapp import MockWhatsAppSender

APP_SECRET = "my_app_secret"
VERIFY_TOKEN = "my_verify_token"
ACCESS_TOKEN = "EAA-test-access-token"
PHONE_NUMBER_ID = "123456789012345"


def _webhook_payload(**overrides) -> dict:
    """A real-shaped Meta webhook POST body (nested entry/changes/value)."""
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WHATSAPP_BUSINESS_ACCOUNT_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "16505551111",
                                "phone_number_id": PHONE_NUMBER_ID,
                            },
                            "contacts": [
                                {
                                    "profile": {"name": "ChemFab"},
                                    "wa_id": "919812345001",
                                }
                            ],
                            "messages": [
                                {
                                    "from": "919812345001",
                                    "id": "wamid.HBgLMTk2Nj",
                                    "timestamp": "1700000000",
                                    "type": "text",
                                    "text": {
                                        "body": "Namaste, 2 drums sulfuric acid chahiye"
                                    },
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    payload.update(overrides)
    return payload


def _body(**overrides) -> bytes:
    return json.dumps(_webhook_payload(**overrides)).encode()


def _handshake(
    mode: str = "subscribe",
    verify_token: str = VERIFY_TOKEN,
    challenge: str = "challenge-12345",
) -> dict[str, str]:
    return {
        "hub.mode": mode,
        "hub.verify_token": verify_token,
        "hub.challenge": challenge,
    }


# --- Inbound parsing ---------------------------------------------------------


def test_parser_reads_nested_meta_webhook_to_neutral_message():
    parser = MetaWhatsAppParser(APP_SECRET, VERIFY_TOKEN)
    messages = parser.parse(method="POST", body=_body())
    assert len(messages) == 1
    message = messages[0]
    # Meta sends `from` as E.164 without the leading plus; the adapter
    # normalizes it so the rest of the system sees "+91..." like every channel.
    assert message.sender == "+919812345001"
    assert message.body == "Namaste, 2 drums sulfuric acid chahiye"
    assert message.media == ()


def test_parser_passes_media_ids_provider_free():
    payload = _webhook_payload()
    payload["entry"][0]["changes"][0]["value"]["messages"][0].update(
        {
            "type": "image",
            "image": {
                "id": "1234567890",
                "mime_type": "image/jpeg",
                "sha256": "abcd",
            },
        }
    )
    messages = MetaWhatsAppParser(APP_SECRET, VERIFY_TOKEN).parse(
        method="POST", body=json.dumps(payload).encode()
    )
    assert len(messages) == 1
    # A media id, not a URL — the MediaFetcher seam owns how the id is fetched.
    assert messages[0].media == ("1234567890",)


def test_parser_returns_empty_without_messages():
    payload = _webhook_payload()
    payload["entry"][0]["changes"][0]["value"]["messages"] = []
    parser = MetaWhatsAppParser(APP_SECRET, VERIFY_TOKEN)
    assert parser.parse(method="POST", body=json.dumps(payload).encode()) == []


def test_parser_returns_empty_on_non_post_or_malformed_body():
    parser = MetaWhatsAppParser(APP_SECRET, VERIFY_TOKEN)
    assert parser.parse(method="GET", body=_body()) == []
    assert parser.parse(method="POST", body=b"not json") == []
    assert parser.parse(method="POST", body=b'{"entry": "no"}') == []


def test_parser_returns_every_message_in_a_delivered_batch():
    payload = _webhook_payload()
    payload["entry"][0]["changes"][0]["value"]["messages"] = [
        {
            "from": "919812345001",
            "id": "wamid.one",
            "timestamp": "1700000000",
            "type": "text",
            "text": {"body": "First order"},
        },
        {
            "from": "919812345002",
            "id": "wamid.two",
            "timestamp": "1700000001",
            "type": "text",
            "text": {"body": "Second order"},
        },
    ]
    messages = MetaWhatsAppParser(APP_SECRET, VERIFY_TOKEN).parse(
        method="POST", body=json.dumps(payload).encode()
    )
    assert [m.sender for m in messages] == ["+919812345001", "+919812345002"]
    assert [m.body for m in messages] == ["First order", "Second order"]


# --- Webhook verification ----------------------------------------------------


def test_signature_matches_meta_algorithm_vector():
    # Worked vector: HMAC-SHA256 of the raw body with the App Secret, hex,
    # prefixed "sha256=", exactly as Meta's X-Hub-Signature-256 is built.
    body = b'{"object":"whatsapp_business_account"}'
    signature = build_meta_signature(body, "my_app_secret")
    assert signature == (
        "sha256=031229137e6e501e64fd10b6a74f06e9c4fc7e22654da45de78cf2299122487a"
    )
    assert verify_meta_signature(body, signature, "my_app_secret")


def test_signature_rejects_wrong_secret_and_tampered_body():
    body = b'{"object":"whatsapp_business_account"}'
    signature = build_meta_signature(body, APP_SECRET)
    assert not verify_meta_signature(body, signature, "wrong-secret")
    assert not verify_meta_signature(b'{"object":"tampered"}', signature, APP_SECRET)


def test_signature_rejects_missing_header_and_empty_secret():
    body = _body()
    signature = build_meta_signature(body, APP_SECRET)
    assert not verify_meta_signature(body, "", APP_SECRET)
    assert not verify_meta_signature(body, signature, "")


def test_parser_verifies_signature_over_the_raw_body():
    # The parser's verify seam checks the exact bytes Meta sent, not a parsed
    # dict — an attacker cannot re-sign a re-encoded body that differs.
    body = _body()
    signature = build_meta_signature(body, APP_SECRET)
    parser = MetaWhatsAppParser(APP_SECRET, VERIFY_TOKEN)
    assert parser.verify_signature(
        method="POST",
        url="http://testserver/api/whatsapp/webhook",
        headers={"X-Hub-Signature-256": signature},
        body=body,
    )
    assert not parser.verify_signature(
        method="POST",
        url="http://testserver/api/whatsapp/webhook",
        headers={"X-Hub-Signature-256": signature},
        body=b"tampered",
    )


def test_handshake_returns_challenge_when_verify_token_matches():
    parser = MetaWhatsAppParser(APP_SECRET, VERIFY_TOKEN)
    assert (
        parser.verification_challenge(method="GET", query=_handshake())
        == "challenge-12345"
    )


def test_handshake_rejects_wrong_token_mode_or_method():
    parser = MetaWhatsAppParser(APP_SECRET, VERIFY_TOKEN)
    assert (
        parser.verification_challenge(
            method="GET", query=_handshake(verify_token="wrong")
        )
        is None
    )
    assert (
        parser.verification_challenge(
            method="GET", query=_handshake(mode="unsubscribe")
        )
        is None
    )
    assert (
        parser.verification_challenge(method="POST", query=_handshake()) is None
    )


# --- Shared Twilio signature (still used by Twilio Voice, issue #10) ---------


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


def test_twilio_signature_rejects_wrong_token():
    params = {"From": "whatsapp:+919812345001", "Body": "x"}
    signature = build_twilio_signature("http://host/webhook", params, "token-a")
    assert not verify_twilio_signature(
        "http://host/webhook", params, signature, "token-b"
    )


def test_twilio_signature_rejects_missing_header_and_empty_token():
    params = {"From": "whatsapp:+919812345001", "Body": "x"}
    assert not verify_twilio_signature("http://host/webhook", params, "", "12345")
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


def _capturing_open(captured: dict):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, limit=-1) -> bytes:
            return b'{"messages":[{"id":"wamid.1"}]}'

    def fake_open(request):
        captured["request"] = request
        return FakeResponse()

    return fake_open


def test_meta_sender_hits_graph_api_messages_endpoint():
    sender = MetaWhatsAppSender(ACCESS_TOKEN, PHONE_NUMBER_ID)
    captured: dict = {}
    sender._open = _capturing_open(captured)  # type: ignore[attr-defined]
    sender.send("+919812345001", "Order confirmed. Estimated total: 35,000 INR.")

    request = captured["request"]
    assert request.full_url == (
        f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    )
    assert request.get_method() == "POST"
    assert request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert request.headers.get("Content-type") == "application/json"
    payload = json.loads(request.data)
    assert payload == {
        "messaging_product": "whatsapp",
        "to": "+919812345001",
        "type": "text",
        "text": {"body": "Order confirmed. Estimated total: 35,000 INR."},
    }


def test_meta_sender_does_not_send_without_credentials():
    sender = MetaWhatsAppSender("", "")
    hit = {"called": False}

    def fake_open(request):
        hit["called"] = True
        return _capturing_open({})(request)

    sender._open = fake_open  # type: ignore[attr-defined]
    sender.send("+919812345001", "hi")
    assert hit["called"] is False


def test_meta_sender_swallows_network_errors():
    sender = MetaWhatsAppSender(ACCESS_TOKEN, PHONE_NUMBER_ID)

    def fake_open(request):
        raise OSError("boom")

    sender._open = fake_open  # type: ignore[attr-defined]
    # The confirmation reply is best-effort — a delivery failure must not
    # surface as a webhook error.
    sender.send("+919812345001", "hi")
