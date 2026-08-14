"""Twilio Voice adapter: the recording-status callback shape (issue #10).

Twilio Voice fires a recording-status callback when a call recording is ready.
The adapter parses the form-encoded callback into a neutral
``VoiceCallRecording``; only a completed recording yields media to understand —
in-progress/failed callbacks are ignored. It also resolves the recording
resource URL to the audio the model can hear (appending the default WAV
extension), so retrieval returns audio rather than JSON metadata. Signature
verification is the shared Twilio algorithm (``src.twilio``), the same HMAC-SHA1
the WhatsApp webhook uses.
"""

from __future__ import annotations

from src.twilio import build_twilio_signature, verify_twilio_signature
from src.twilio_voice import TwilioVoiceCallbackParser

CALLBACK_URL = "https://valence.example/api/voice/callback"

RECORDING_URL = (
    "https://api.twilio.com/2010-04-01/Accounts/ACxxxx/Recordings/RErecording01"
)

COMPLETED = {
    "CallSid": "CA11111111111111111111111111111111",
    "From": "+919812345001",
    "To": "+14159998888",
    "RecordingSid": "RErecording01",
    "RecordingUrl": RECORDING_URL,
    "RecordingStatus": "completed",
    "RecordingDuration": "42",
}


def test_parser_reads_completed_recording():
    recording = TwilioVoiceCallbackParser().parse(COMPLETED)
    assert recording is not None
    assert recording.caller == "+919812345001"
    # The resource URL is resolved to an audio URL so retrieval returns audio,
    # not the recording resource's JSON metadata.
    assert recording.recording_url == RECORDING_URL + ".wav"


def test_parser_keeps_existing_media_extension():
    for url in (RECORDING_URL + ".mp3", RECORDING_URL + ".wav"):
        form = dict(COMPLETED, RecordingUrl=url)
        assert TwilioVoiceCallbackParser().parse(form).recording_url == url


def test_parser_ignores_non_completed_status():
    for status in ("in-progress", "failed", "absent"):
        form = dict(COMPLETED, RecordingStatus=status)
        assert TwilioVoiceCallbackParser().parse(form) is None


def test_parser_returns_none_without_caller():
    form = dict(COMPLETED)
    del form["From"]
    assert TwilioVoiceCallbackParser().parse(form) is None


def test_parser_returns_none_without_recording_url():
    form = dict(COMPLETED)
    del form["RecordingUrl"]
    assert TwilioVoiceCallbackParser().parse(form) is None


def test_voice_callback_uses_shared_signature_algorithm():
    # A recording-status callback is signed with the same X-Twilio-Signature
    # HMAC-SHA1 algorithm as the WhatsApp webhook — one verifier, both channels.
    signature = build_twilio_signature(CALLBACK_URL, COMPLETED, "tok")
    assert verify_twilio_signature(CALLBACK_URL, COMPLETED, signature, "tok")
    assert not verify_twilio_signature(CALLBACK_URL, COMPLETED, signature, "other")
