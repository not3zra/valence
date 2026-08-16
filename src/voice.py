"""The voice channel boundary: the neutral call-recording shape.

The recording-status callback (issue #10) is a provider boundary. The webhook
only ever sees the neutral ``VoiceCallRecording`` — the caller identity and the
authenticated URL the audio is fetched from — while the Twilio-specific parsing
lives in ``src.twilio_voice``, mirroring the WhatsApp split between
``src.whatsapp`` (neutral shape + seam) and ``src.meta_whatsapp`` (adapter).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class VoiceCallRecording:
    """A completed call recording, provider-neutral.

    ``caller`` is the E.164 phone number of the caller — the session identity a
    voice order is credited to, exactly like the WhatsApp sender number.
    ``recording_url`` is the authenticated Twilio URL the audio is fetched from
    through the ``MediaFetcher`` seam.
    """

    caller: str
    recording_url: str


class VoiceCallbackParser(Protocol):
    """Inbound seam: turn a provider's recording-status callback into a recording.

    Returning ``None`` means the callback carried no completed recording to
    process (non-completed status, or missing identity/URL).
    """

    def parse(self, form: dict[str, str]) -> VoiceCallRecording | None: ...
