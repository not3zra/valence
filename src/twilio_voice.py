"""Twilio Voice adapter: the recording-status callback parser.

Everything Twilio-specific about the recording-status callback (issue #10)
lives here, behind the seam defined in ``src.voice``. Only a completed
recording yields media to understand; in-progress/failed callbacks are ignored,
matching ADR-0004 (call orders are never clarified, so a partial transcript
from an in-progress recording would be noise).

The adapter also resolves the callback's recording resource URL to the audio
the model can hear: Twilio's ``RecordingUrl`` points at the recording resource
whose default representation is JSON metadata, so the adapter appends the
default WAV media extension (``.wav``) unless the URL already names a media
format. Retrieving audio then goes through the same authenticated, allowlisted
``MediaFetcher`` as photo intake.
"""

from __future__ import annotations

from urllib.parse import urlparse

from .voice import VoiceCallRecording

# Twilio's default call-recording media format is WAV; a recording resource URL
# with no media extension is turned into an audio URL by appending this suffix.
DEFAULT_RECORDING_FORMAT = ".wav"

# Extensions that already denote a concrete media format and are left as-is.
_AUDIO_EXTENSIONS = (".wav", ".mp3", ".amr", ".mp4")


class TwilioVoiceCallbackParser:
    """Parse a Twilio Voice recording-status callback's form fields."""

    def parse(self, form: dict[str, str]) -> VoiceCallRecording | None:
        if (form.get("RecordingStatus") or "").strip().lower() != "completed":
            return None
        caller = (form.get("From") or "").strip()
        recording_url = self._audio_url(form.get("RecordingUrl") or "")
        if not caller or not recording_url:
            return None
        return VoiceCallRecording(caller=caller, recording_url=recording_url)

    @staticmethod
    def _audio_url(url: str) -> str:
        """Return ``url`` as an audio URL, appending the default WAV extension
        when the resource path does not already name a media format."""
        try:
            path = urlparse(url).path
        except ValueError:
            return ""
        if not url or path.endswith(_AUDIO_EXTENSIONS):
            return url
        return url + DEFAULT_RECORDING_FORMAT
