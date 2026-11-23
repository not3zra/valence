"""Run every agent turn on one long-lived event loop.

ADK's ``Runner`` drives the session service over async I/O, and its sync
``Runner.run()`` wraps each turn in a throwaway event loop. A durable session
service's async client binds to whichever loop first uses it; a web server
closes a request's loop when the request ends, so the second turn through the
same service crashes with ``RuntimeError: Event loop is closed``. This executor
pins one persistent event loop in a dedicated thread, builds the agent, session
service, and runner on it, and schedules every turn onto it.
"""

from __future__ import annotations

import asyncio
import threading

from google.adk.runners import Runner
from google.genai import types

from .agent import _event_text, build_runner
from .media import MediaObject, media_to_inline_part
from .model_provider import ModelCapabilities, build_model_provider


class TurnExecutor:
    """Owns the persistent event loop, the ADK runner, and every agent turn.

    The zero-argument factory is called inside the loop thread, so the agent and
    session service are built there and a turn can be scheduled from any thread
    — a sync handler's threadpool, or an async handler's request loop — and
    still run against a live loop.
    """

    def __init__(
        self,
        agent_or_factory,
        session_service=None,
        capabilities: ModelCapabilities | None = None,
    ) -> None:
        if session_service is not None or not callable(agent_or_factory):
            # Legacy path: (agent, session_service) positional args for tests.
            _agent = agent_or_factory
            _session_service = session_service

            def _factory():
                return _agent, _session_service

            self._agent_factory = _factory
        else:
            self._agent_factory = agent_or_factory
        self._capabilities = (
            capabilities
            if capabilities is not None
            else build_model_provider().capabilities
        )
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

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    def _bootstrap(self) -> None:
        try:
            asyncio.set_event_loop(self._loop)
            agent, session_service = self._agent_factory()
            self._runner = build_runner(agent, session_service)
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
        if media is not None and self._capabilities.accepts(media):
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

    async def run_turn_async(
        self, *, sender_id: str, message: str, media: MediaObject | None = None
    ) -> str:
        """Run one turn without blocking the caller's event loop.

        The coroutine still runs on the persistent executor loop (same durable
        session semantics as :meth:`run_turn`), but awaiting the wrapped future
        lets callers apply a real deadline: cancelling the await cancels the
        turn on the executor loop too.
        """
        future = asyncio.run_coroutine_threadsafe(
            self._run_turn_async(sender_id=sender_id, message=message, media=media),
            self._loop,
        )
        return await asyncio.wrap_future(future)

    def stop(self) -> None:
        """Stop the loop and join the thread (test teardown; the process exit
        handles this for the deployed app)."""
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)
