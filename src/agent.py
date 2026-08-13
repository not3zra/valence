"""The single ADK agent and the durable-session plumbing.

One ADK Agent is the unified understanding layer for every intake channel
(ADR-0001). It holds one durable session per sender (keyed by the sender id),
calls Gemini to extract a structured order, and exposes the Order Processing
Core as its single ``process_order`` tool (ticket 2, #3). Sessions persist to
Firestore via ADK's FirestoreSessionService so a Cloud Run restart never loses
an in-flight clarifying conversation.
"""

from __future__ import annotations

from google.adk.agents import Agent
from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService
from google.adk.integrations.firestore.firestore_session_service import (
    FirestoreSessionService,
)
from google.adk.models import BaseLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from .config import settings
from .core import OrderProcessingCore
from .core_tool import build_process_order_tool

AGENT_INSTRUCTION = """You are Valence, the order intake agent for a chemical \
distributor. A customer sends an order over WhatsApp, a phone call, or a photo \
of a handwritten order sheet, in any language. Understand the message as a \
structured order and commit it by calling the process_order tool. Structured \
extraction is wired from ticket 3 (#4); reply in the customer's language and \
keep it short."""


def build_agent(model: str | BaseLlm | None = None, store=None) -> Agent:
    """Build the root ADK agent.

    ``model`` defaults to the configured Gemini model id and resolves through
    ADK's LLM registry to the Gemini API. Tests inject a fake ``BaseLlm`` here
    so the whole runner wiring runs without a network call (ADR testing seam).
    ``store`` wires the Order Processing Core as the agent's single
    ``process_order`` tool; tests pass the in-memory store so the tool runs
    without Firestore.
    """
    tools: list = []
    if store is not None:
        tools.append(build_process_order_tool(OrderProcessingCore(store)))
    return Agent(
        name=settings.app_name,
        model=model or settings.gemini_model,
        instruction=AGENT_INSTRUCTION,
        tools=tools,
    )


def build_session_service():
    """Build the durable session service.

    Production and the local Firestore emulator both use ADK's
    FirestoreSessionService; the AsyncClient picks up ``FIRESTORE_EMULATOR_HOST``
    automatically. ``SESSION_SERVICE=memory`` swaps in the in-memory service for
    fast local debugging (sessions do not survive a restart there).
    """
    if settings.session_service == "memory":
        return InMemorySessionService()
    return FirestoreSessionService(root_collection=settings.firestore_root_collection)


def build_runner(agent: Agent, session_service) -> Runner:
    """Build an ADK Runner wired to the durable session service.

    ``auto_create_session`` lets an inbound message from a sender start their
    session lazily; the same session id (the sender id) is reused across turns,
    which is what keeps a clarifying conversation alive across webhook calls.
    """
    return Runner(
        app_name=settings.app_name,
        agent=agent,
        session_service=session_service,
        artifact_service=InMemoryArtifactService(),
        auto_create_session=True,
    )


def _event_text(event) -> str:
    if not event.content or not event.content.parts:
        return ""
    return "".join(part.text or "" for part in event.content.parts if part.text)


def run_turn(runner: Runner, *, sender_id: str, message: str) -> str:
    """Run one agent turn for ``sender_id`` and return the final reply text.

    The session id is the sender id, so consecutive messages from the same
    sender continue the same durable session (ADR-0001: one session keyed per
    WhatsApp sender).
    """
    content = types.Content(role="user", parts=[types.Part(text=message)])
    for event in runner.run(
        user_id=sender_id,
        session_id=sender_id,
        new_message=content,
    ):
        if event.is_final_response():
            return _event_text(event)
    return ""
