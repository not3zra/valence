"""Run every agent turn on one long-lived event loop.

ADK's ``Runner`` drives the session service over async I/O, and its sync
``Runner.run()`` wraps each turn in a throwaway event loop. The
``FirestoreSessionService``'s async gRPC client binds to whichever loop first
uses it; a web server closes a request's loop when the request ends, so the
second turn through the same service crashes with ``RuntimeError: Event loop is
closed``. This executor pins one persistent event loop in a dedicated thread,
builds the ADK runner on it, and schedules every turn onto it — the durable
session service is only ever touched from a loop that outlives the process.
"""

from __future__ import annotations

import asyncio
import threading

from google.adk.agents import Agent
from google.adk.runners import Runner
from google.genai import types

from .agent import _event_text, build_runner
from .media import MediaObject, media_to_inline_part


class TurnExecutor:
    """Owns the persistent event loop, the ADK runner, and every agent turn.

    Constructing the runner inside the loop thread means the session service's
    async client binds to that loop, so a turn can be scheduled from any thread
    — a sync handler's threadpool, or an async handler's request loop — and
    still run against a live loop.
    """

    def __init__(self, agent: Agent, session_service) -> None:
        self._agent = agent
        self._session_service = session_service
        self._loop = asyncio.new_event_loop()
        self._runner: Runner | None = None
        self._error: BaseException | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._bootstrap, name="valence-agent-loop", daemon=True
        )
        self._thread.start()
        self._ready.wait()
        if self._error is not None:
            raise self._error

    def _bootstrap(self) -> None:
        try:
            asyncio.set_event_loop(self._loop)
            self._runner = build_runner(self._agent, self._session_service)
        except BaseException as exc:  # pragma: no cover - defensive startup guard
            self._error = exc
        finally:
            self._ready.set()
        if self._error is None:
            self._loop.run_forever()

    async def _run_turn_async(
        self, *, sender_id: str, message: str, media: MediaObject | None
    ) -> str:
        runner = self._runner
        assert runner is not None
        parts = [types.Part(text=message)]
        if media is not None:
            parts.append(media_to_inline_part(media))
        content = types.Content(role="user", parts=parts)
        reply = ""
        async for event in runner.run_async(
            user_id=sender_id,
            session_id=sender_id,
            new_message=content,
        ):
            if event.is_final_response():
                reply = _event_text(event)
        return reply

    def run_turn(
        self, *, sender_id: str, message: str, media: MediaObject | None = None
    ) -> str:
        """Run one agent turn for ``sender_id`` and return the final reply.

        Blocks the calling thread until the turn completes on the executor loop
        (the same semantics as the sync ``agent.run_turn`` seam, issue #4). The
        session id is the sender id, so consecutive messages from the same
        sender continue the same durable session.
        """
        future = asyncio.run_coroutine_threadsafe(
            self._run_turn_async(sender_id=sender_id, message=message, media=media),
            self._loop,
        )
        return future.result()

    def stop(self) -> None:
        """Stop the loop and join the thread (test teardown; the process exit
        handles this for the deployed app)."""
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)
