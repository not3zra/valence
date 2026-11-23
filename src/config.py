"""Runtime configuration, read from the environment once at import time."""

from __future__ import annotations

import os


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


class Settings:
    """Process-level settings.

    The defaults keep the service runnable in a fresh clone without Google
    Cloud: sessions and orders stay in memory, and vouchers use the in-memory
    store. For a durable local Instance without GCP, set ``ORDER_STORE=sqlite``,
    ``SESSION_SERVICE=sqlite``, ``DATABASE_URL`` and ``VOUCHER_DIR``. Use
    ``postgres`` for both store and sessions with a ``postgresql+asyncpg://``
    ``DATABASE_URL`` (the Compose profile). The optional ``firestore`` adapters
    remain available with the ``[gcp]`` extra.
    """

    def __init__(self) -> None:
        self.app_name: str = _env("APP_NAME", "valence")
        self.model_provider: str = _env("MODEL_PROVIDER", "gemini")
        self.gemini_model: str = _env("GEMINI_MODEL", "gemini-3.5-flash")
        self.session_service: str = _env("SESSION_SERVICE", "memory")
        self.order_store: str = _env("ORDER_STORE", "")
        self.database_url: str = _env(
            "DATABASE_URL", "sqlite+aiosqlite:///./valence.db"
        )
        self.firestore_root_collection: str = _env(
            "ADK_FIRESTORE_ROOT_COLLECTION", "adk-session"
        )
        self.service_port: int = int(_env("PORT", "8080"))
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
        self.voucher_dir: str = _env("VOUCHER_DIR", "")
        self.webhook_rate_limit: int = int(
            _env("WEBHOOK_RATE_LIMIT_PER_SENDER", "30")
        )
        self.agent_turn_timeout_s: float = float(_env("AGENT_TURN_TIMEOUT_S", "120"))
        self.tally_push_url: str = _env("TALLY_PUSH_URL", "")
        self.service_url: str = _env("SERVICE_URL", "")
        self.valence_api_key: str = _env("VALENCE_API_KEY", "")
        self.setup_token: str = _env("VALENCE_SETUP_TOKEN", "")


settings = Settings()
