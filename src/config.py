"""Runtime configuration, read from the environment once at import time."""

from __future__ import annotations

import os


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


class Settings:
    """Process-level settings.

    The defaults keep the service runnable in a fresh clone with nothing but a
    Google API key: the Gemini model id resolves through ADK's LLM registry and
    the Firestore session service points at the emulator when
    ``FIRESTORE_EMULATOR_HOST`` is set.
    """

    def __init__(self) -> None:
        self.app_name: str = _env("APP_NAME", "valence")
        self.gemini_model: str = _env("GEMINI_MODEL", "gemini-3.5-flash")
        self.session_service: str = _env("SESSION_SERVICE", "firestore")
        self.firestore_root_collection: str = _env(
            "ADK_FIRESTORE_ROOT_COLLECTION", "adk-session"
        )
        self.service_port: int = int(_env("PORT", "8080"))
        self.twilio_account_sid: str = _env("TWILIO_ACCOUNT_SID", "")
        self.twilio_auth_token: str = _env("TWILIO_AUTH_TOKEN", "")
        self.meta_app_secret: str = _env("META_APP_SECRET", "")
        self.meta_access_token: str = _env("META_ACCESS_TOKEN", "")
        self.meta_phone_number_id: str = _env("META_PHONE_NUMBER_ID", "")
        self.meta_verify_token: str = _env("META_VERIFY_TOKEN", "")
        self.roundtrip_token: str = _env("ROUNDTRIP_TOKEN", "")
        self.voice_ingest_token: str = _env("VOICE_INGEST_TOKEN", "")
        self.web_passcode: str = _env("WEB_PASSCODE", "")
        self.web_passcode_salt: str = _env("WEB_PASSCODE_SALT", "")
        self.web_cookie_secure: bool = _env("WEB_COOKIE_SECURE", "1") != "0"
        self.cutoff_secret: str = _env("CUTOFF_SECRET", "")
        self.voucher_bucket: str = _env("VOUCHER_BUCKET", "")
        self.webhook_rate_limit: int = int(
            _env("WEBHOOK_RATE_LIMIT_PER_SENDER", "30")
        )


settings = Settings()
