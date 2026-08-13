"""Shared test doubles.

ADK's model boundary is faked per the project's testing decisions: Gemini is a
boundary adapter, faked in tests, so the runner wiring is exercised without any
network call.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types


class FakeEchoLlm(BaseLlm):
    """Echoes the last user part back, prefixed, with a fixed system nudge."""

    model: str = "fake-echo"

    def supported_models(self) -> list[str]:
        return ["fake-echo.*"]

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        last_user = ""
        for content in llm_request.contents:
            if content.role == "user":
                last_user = "".join(
                    part.text or "" for part in content.parts if part.text
                )
        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part(text=f"Echo: {last_user}")],
            )
        )
