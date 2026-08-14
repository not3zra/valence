"""Expose the Order Processing Core to the ADK agent as a single tool."""

from __future__ import annotations

from google.adk.tools import FunctionTool, ToolContext

from .approval import ApprovalNotifier
from .core import ApprovalError, OrderProcessingCore
from .orders import Order, OrderItem, utcnow

# Session-state key that carries a partial order + clarify turn count across the
# durable per-sender session (issue #5). Held in ADK session state, so a Cloud
# Run restart never loses an in-flight clarifying conversation.
CLARIFY_STATE_KEY = "valence_clarify"


def _read_pending(state) -> dict | None:
    try:
        return state.get(CLARIFY_STATE_KEY)
    except (AttributeError, KeyError):
        return None


def _hours_since(start_iso: str, end_iso: str) -> float:
    from datetime import datetime, timezone

    start = datetime.fromisoformat(start_iso)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(end_iso)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return (end - start).total_seconds() / 3600


def build_process_order_tool(
    core: OrderProcessingCore, notifier: ApprovalNotifier | None = None
) -> FunctionTool:
    """Wrap the core as a single ``process_order`` ADK tool.

    The agent invokes it with the structured order extracted by Gemini and reads
    back the decision — approved flag, status, escalation reasons, and the draft
    estimate. The order's phone is pinned to the caller's session identity
    (ADR-0002: identity comes from the verified channel number, never from the
    message the model reads), so extraction carries nothing that can credit a
    customer. The store behind the core is whatever the core was built with:
    Firestore in production, the in-memory double in tests.

    ``notifier``, when supplied, is called for every escalation this tool
    produces (issue #7): it tells every allowlisted approver the order needs a
    yes/no decision. It is intentionally a separate seam so the tool stays
    channel-agnostic and the notification is testable in isolation.
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

        state = (
            getattr(tool_context, "state", None)
            if tool_context is not None
            else None
        )
        pending = _read_pending(state)

        # A pending partial order older than the configured timeout is promoted
        # to escalation before this new message is handled (issue #5): the
        # customer never answered the clarifying question.
        if pending is not None:
            policy = await core.clarify_policy()
            try:
                elapsed = _hours_since(pending["created_at"], utcnow())
            except (KeyError, TypeError, ValueError):
                elapsed = 0.0
            if elapsed >= policy["clarify_timeout_hours"]:
                await core.process(
                    Order.from_dict(pending["order"]), clarify=False
                )
                if state is not None:
                    state[CLARIFY_STATE_KEY] = None
                pending = None

        decision = await core.process(order)

        if decision.clarify:
            turn = (pending["turn"] if pending is not None else 0) + 1
            policy = await core.clarify_policy()
            if turn > policy["clarify_turn_cap"]:
                # The loop ran past its turn cap -> hand the held partial order
                # to a human as a flagged escalation (issue #5).
                held = pending["order"] if pending is not None else order.to_dict()
                decision = await core.process(Order.from_dict(held), clarify=False)
                if state is not None:
                    state[CLARIFY_STATE_KEY] = None
            elif state is not None:
                state[CLARIFY_STATE_KEY] = {
                    "order": order.to_dict(),
                    "turn": turn,
                    "created_at": utcnow(),
                }
                return decision.to_dict()

        # The order committed (approved / escalated / duplicate): a fresh order,
        # so any held partial clarify state is no longer current.
        if state is not None and _read_pending(state) is not None:
            state[CLARIFY_STATE_KEY] = None

        # An escalated order needs a human yes/no decision (issue #7). The
        # notifier is a channel-agnostic seam: it tells every allowlisted
        # approver and records the request on the audit trail. This fires once
        # per committed escalation, including a clarify loop that promoted to
        # escalation on its final turn.
        if notifier is not None and not decision.approved and not decision.duplicate:
            await notifier.on_order_escalated(decision.order_id)

        return decision.to_dict()

    return FunctionTool(process_order)


def build_approve_order_tool(core: OrderProcessingCore, store) -> FunctionTool:
    """Wrap the human approval path as an ``approve_order`` ADK tool.

    An allowlisted approver's WhatsApp reply resolves to exactly one pending
    order: the pending-approval registry keys approver phone -> order id (issue
    #7). The tool reads that id back from the store, applies the yes/no
    decision through the core (which re-checks the allowlist), and clears the
    pending entry so a later reply cannot act twice — for this and every other
    approver who was asked to decide the same order. A caller with nothing
    pending — a non-allowlisted number, or an approver with no open request —
    gets an error dict the agent is told to ignore, so it can never act on an
    order it was not invited to decide.
    """

    async def approve_order(
        approved: bool,
        tool_context: ToolContext | None = None,
    ) -> dict:
        """Apply an approver's yes/no decision to the order awaiting them.

        Args:
            approved: True to approve the order, False to reject it.
            tool_context: ADK invocation context; the approver's phone is the
                caller's session identity, never anything from the message.

        Returns:
            The core's decision (status, approved flag) or an error dict.
        """
        phone = tool_context.user_id if tool_context is not None else None
        if not phone:
            return {"error": "no verified sender identity is available"}

        order_id = await store.get_pending_approval(phone)
        if order_id is None:
            return {"error": f"{phone} has no order awaiting approval"}

        try:
            decision = await core.approve_order(
                order_id, approved=approved, by_phone=phone
            )
        except ApprovalError as exc:
            return {"error": str(exc)}

        await store.clear_pending_approvals_for_order(decision.order_id)
        return decision.to_dict()

    return FunctionTool(approve_order)
