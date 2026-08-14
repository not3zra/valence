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
    they are under approval; a ``duplicate`` decision is answered with an
    "already received" message, issue #12). Turn-aware: it only answers a tool
    result that arrived after the most recent inbound text, so a repeated
    message in the same durable session triggers a fresh tool call instead of
    re-answering the previous turn's result.
    """

    model: str = "fake-confirm"

    def supported_models(self) -> list[str]:
        return ["fake-confirm.*"]

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        contents = llm_request.contents
        last_inbound = -1
        last_result = -1
        for i, content in enumerate(contents):
            if content.role == "user" and not any(
                part.function_response for part in content.parts
            ):
                last_inbound = i
            elif content.role == "user":
                last_result = i

        if last_result > last_inbound:
            result = contents[last_result].parts[0].function_response.response
            duplicate = bool(result.get("duplicate"))
            draft_value_inr = float(result["draft_value_inr"])
            approved = bool(result["approved"])

            if duplicate:
                text = "Your order has already been received — no need to resend."
            elif approved:
                text = f"Order confirmed. Estimated total: {draft_value_inr:,.0f} INR."
            else:
                total = f"{draft_value_inr:,.0f} INR."
                text = f"Your order is under approval. Estimated total: {total}"
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
