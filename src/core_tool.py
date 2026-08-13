"""Expose the Order Processing Core to the ADK agent as a single tool."""

from __future__ import annotations

from google.adk.tools import FunctionTool, ToolContext

from .core import OrderProcessingCore
from .orders import Order, OrderItem


def build_process_order_tool(core: OrderProcessingCore) -> FunctionTool:
    """Wrap the core as a single ``process_order`` ADK tool.

    The agent invokes it with the structured order extracted by Gemini and reads
    back the decision — approved flag, status, escalation reasons, and the draft
    estimate. The order's phone is pinned to the caller's session identity
    (ADR-0002: identity comes from the verified channel number, never from the
    message the model reads), so extraction carries nothing that can credit a
    customer. The store behind the core is whatever the core was built with:
    Firestore in production, the in-memory double in tests.
    """

    async def process_order(
        items: list[dict],
        confidence: float,
        customer: str | None = None,
        delivery_location: str | None = None,
        source_language: str = "en",
        source_channel: str = "whatsapp",
        tool_context: ToolContext | None = None,
    ) -> dict:
        """Commit a structured order through the Order Processing Core.

        Args:
            items: Order lines, each with a product, quantity, unit, and an
                optional rate_inr stated by the customer.
            confidence: Extraction confidence in (0, 1].
            customer: Extracted customer name or id, if any.
            delivery_location: Extracted delivery location name, if any.
            source_language: BCP-47 tag of the sender's language.
            source_channel: Intake channel (whatsapp, phone, or photo).
            tool_context: ADK invocation context; the order's phone is taken
                from the caller's session identity here, never from the message.

        Returns:
            The core's decision: approved flag, status, escalation reasons,
            draft estimate, and resolved items.
        """
        phone = tool_context.user_id if tool_context is not None else None
        if not phone:
            return {"error": "no verified sender identity is available"}
        try:
            order = Order(
                phone=phone,
                customer=customer,
                delivery_location=delivery_location,
                confidence=confidence,
                source_language=source_language,
                source_channel=source_channel,
                items=[OrderItem.from_dict(item) for item in items],
            )
        except (TypeError, ValueError):
            return {"error": "could not parse the order"}
        decision = await core.process(order)
        return decision.to_dict()

    return FunctionTool(process_order)
