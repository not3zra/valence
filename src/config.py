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


settings = Settings()
