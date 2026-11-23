"""Capability-aware model-provider seam (ADR-0008).

A provider binds one agent-runtime model (an ADK model id or ``BaseLlm``) to the
capabilities of the service behind it. The default is native Gemini through
ADK's built-in adapter — no LiteLLM translation layer on the default path — and
a fake provider keeps the whole runtime testable without a network call.
Provider selection comes from ``MODEL_PROVIDER``; adding a provider means adding
one adapter here, not changing the agent or the intake paths.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Protocol

from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from .config import settings
from .media import MediaObject

_APPROVAL_REPLY = "Your order is under approval."


@dataclass(frozen=True)
class ModelCapabilities:
    """What the selected provider can understand in an agent turn.

    ``text`` is always available: every provider in the protocol handles the
    text order flow. Image and audio gate photo and voice intake respectively;
    ``response_schema`` is the ADR-0008 name for the structured-output flag.
    """

    text: bool = True
    image: bool = True
    audio: bool = True
    response_schema: bool = True

    def accepts(self, media: MediaObject) -> bool:
        """Whether this provider can understand an inbound media object."""
        mime = media.mime_type.lower()
        if mime.startswith("image/"):
            return self.image
        if mime.startswith("audio/"):
            return self.audio
        return True


class ModelProvider(Protocol):
    """The model and its capabilities, resolved from configuration."""

    @property
    def model(self) -> str | BaseLlm: ...

    @property
    def capabilities(self) -> ModelCapabilities: ...


class GeminiModelProvider:
    """Native Gemini model id resolved by ADK's own Gemini adapter."""

    def __init__(self, model: str | None = None) -> None:
        self._model = model if model is not None else settings.gemini_model

    @property
    def model(self) -> str:
        return self._model

    @property
    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities()


class FakeEchoLlm(BaseLlm):
    """Echoes the inbound text so a fake-provider agent turn needs no network."""

    model: str = "fake-provider"

    @classmethod
    def supported_models(cls) -> list[str]:
        return ["fake-provider.*"]

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        last_user = ""
        for content in llm_request.contents or []:
            if content.role == "user":
                last_user = "".join(
                    part.text or "" for part in content.parts or [] if part.text
                )
        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part(text=f"Echo: {last_user}")],
            )
        )


class FakeToolCallingLlm(BaseLlm):
    """Commits a deterministic order through the real ``process_order`` tool.

    Unlike :class:`FakeEchoLlm`, this model drives the full ADK tool path, so an
    offline process can ingest, approve, and mark a real order billed. The confidence
    is below the seeded minimum, leaving the order in ``pending_review`` for the
    Agent Interface's confirmed approve action. Turn-aware like the test fakes:
    a tool result newer than the inbound text gets a final reply instead of a
    second commit.
    """

    model: str = "fake-toolcall"

    @classmethod
    def supported_models(cls) -> list[str]:
        return ["fake-toolcall.*"]

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        contents = llm_request.contents or []
        last_inbound = -1
        last_result = -1
        for index, content in enumerate(contents):
            has_result = any(part.function_response for part in content.parts or [])
            if content.role == "user" and has_result:
                last_result = index
            elif content.role == "user":
                last_inbound = index
        if last_result > last_inbound:
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=_APPROVAL_REPLY)],
                )
            )
            return

        call = types.FunctionCall(
            name="process_order",
            args={
                "items": [{"product": "sulfuric acid", "quantity": 2000, "unit": "kg"}],
                "confidence": 0.5,
                "delivery_location": "Peenya Industrial Area",
            },
        )
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part(function_call=call)])
        )
        yield LlmResponse(
            content=types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            name="process_order", response={"ok": "committed"}
                        )
                    )
                ],
            )
        )
        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part(text=_APPROVAL_REPLY)],
            )
        )


class FakeModelProvider:
    """Provider double for tests; capabilities can be narrowed per case."""

    def __init__(
        self,
        model: BaseLlm | None = None,
        capabilities: ModelCapabilities | None = None,
    ) -> None:
        self._model = model if model is not None else FakeEchoLlm()
        self._capabilities = (
            capabilities if capabilities is not None else ModelCapabilities()
        )

    @property
    def model(self) -> BaseLlm:
        return self._model

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._capabilities


class FakeToolCallProvider(FakeModelProvider):
    """Offline provider that commits orders instead of echoing the inbound text."""

    def __init__(
        self,
        model: BaseLlm | None = None,
        capabilities: ModelCapabilities | None = None,
    ) -> None:
        super().__init__(
            model if model is not None else FakeToolCallingLlm(), capabilities
        )


_PROVIDERS: dict[str, type[GeminiModelProvider] | type[FakeModelProvider]] = {
    "gemini": GeminiModelProvider,
    "fake": FakeModelProvider,
    "fake-toolcall": FakeToolCallProvider,
}


def build_model_provider(name: str | None = None) -> ModelProvider:
    """Build the configured provider, or ``name`` when explicitly selected."""
    selected = name if name is not None else settings.model_provider
    try:
        provider_type = _PROVIDERS[selected]
    except KeyError:
        expected = ", ".join(sorted(_PROVIDERS))
        raise ValueError(
            f"Unknown MODEL_PROVIDER {selected!r}; expected one of: {expected}"
        ) from None
    return provider_type()
