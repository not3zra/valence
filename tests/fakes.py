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


class ToolCallingLlm(BaseLlm):
    """Calls ``process_order`` once with a fixed clean order, then replies.

    Exercises the real ADK tool path — declaration, model tool call, injected
    tool context, tool result — without Gemini or Firestore.
    """

    model: str = "fake-toolcall"

    def supported_models(self) -> list[str]:
        return ["fake-toolcall.*"]

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        call = types.FunctionCall(
            name="process_order",
            args={
                "items": [
                    {"product": "sulfuric acid", "quantity": 2000, "unit": "kg"}
                ],
                "confidence": 0.9,
                "delivery_location": "Peenya Industrial Area",
            },
        )
        model_part = types.Content(role="model", parts=[types.Part(function_call=call)])
        function_response = types.Part(
            function_response=types.FunctionResponse(
                name="process_order", response={"ok": "committed"}
            )
        )
        tool_part = types.Content(role="user", parts=[function_response])
        done = types.Content(role="model", parts=[types.Part(text="Order committed.")])
        for content in [model_part, tool_part, done]:
            yield LlmResponse(content=content)


class ConfirmingToolCallingLlm(BaseLlm):
    """Calls ``process_order`` once, then confirms with the estimated total.

    Reads the real tool result (the function response ADK feeds back on the
    second turn, which carries the core's ``draft_value_inr`` and ``approved``
    flag) and composes a confirmation that reflects it — the behavior the agent
    instruction pins for the WhatsApp channel (issue #4: the confirmation reply
    includes the estimated total from draft pricing; escalated orders are told
    they are under approval).
    """

    model: str = "fake-confirm"

    def supported_models(self) -> list[str]:
        return ["fake-confirm.*"]

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        draft_value_inr: float | None = None
        approved: bool | None = None
        for content in llm_request.contents:
            for part in content.parts:
                fr = part.function_response
                if fr and isinstance(fr.response, dict):
                    if "draft_value_inr" in fr.response:
                        draft_value_inr = float(fr.response["draft_value_inr"])
                    if "approved" in fr.response:
                        approved = bool(fr.response["approved"])

        if draft_value_inr is not None:
            total = f"{draft_value_inr:,.0f} INR."
            if approved is False:
                text = f"Your order is under approval. Estimated total: {total}"
            else:
                text = f"Order confirmed. Estimated total: {total}"
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=text)],
                )
            )
            return

        call = types.FunctionCall(
            name="process_order",
            args={
                "items": [
                    {"product": "sulfuric acid", "quantity": 2000, "unit": "kg"}
                ],
                "confidence": 0.9,
                "delivery_location": "Peenya Industrial Area",
            },
        )
        yield LlmResponse(
            content=types.Content(
                role="model", parts=[types.Part(function_call=call)]
            )
        )
