"""The TurnExecutor seam that keeps ADK's runner on one live event loop.

Regression coverage for the live-wiring bug (#38): ADK's sync ``Runner.run()``
wraps each turn in a fresh ``asyncio.run`` loop, while a loop-bound session
service (ADK's ``FirestoreSessionService``) caches its async client on the
first loop that uses it — so the second turn through the old path failed with
``RuntimeError: Event loop is closed``. The ``TurnExecutor`` pins one
long-lived loop and runs every turn on it; these tests prove the contract.
"""

from __future__ import annotations

import asyncio

import pytest
from google.adk.sessions import InMemorySessionService

from src.agent import build_agent, build_runner, run_turn
from src.agent_exec import TurnExecutor
from src.config import settings

from .fakes import FakeEchoLlm


class _LoopBoundSessionService(InMemorySessionService):
    """Simulates ADK's FirestoreSessionService for the closed-loop failure.

    The real service's async gRPC client binds to whichever event loop first
    uses it and keeps using that (now closed) loop on the next turn. This fake
    captures the same failure deterministically, so the tests need neither
    Firestore nor the network: the first loop to touch it is remembered, and a
    touch from a closed loop raises the exact production error.
    """

    def __init__(self) -> None:
        super().__init__()
        self._bound_loop: asyncio.AbstractEventLoop | None = None
        self.touched_loops: list[asyncio.AbstractEventLoop] = []

    def _touch(self) -> None:
        loop = asyncio.get_running_loop()
        self.touched_loops.append(loop)
        if self._bound_loop is None:
            self._bound_loop = loop
        if self._bound_loop.is_closed():
            raise RuntimeError("Event loop is closed")

    async def create_session(self, **kwargs):
        self._touch()
        return await super().create_session(**kwargs)

    async def append_event(self, *args, **kwargs):
        self._touch()
        return await super().append_event(*args, **kwargs)

    async def get_session(self, *args, **kwargs):
        self._touch()
        return await super().get_session(*args, **kwargs)


@pytest.fixture
def echo_agent():
    return build_agent(model=FakeEchoLlm())


def test_sync_run_turn_fails_again_after_first_turn(echo_agent):
    # The bug, documented (issue #38): ADK's sync runner spins up a throwaway
    # asyncio.run loop per turn, and a loop-bound session service is left
    # pointing at a loop that closes once the turn is done — so the second turn
    # silently produces no reply. That is exactly the live symptom seen before
    # the fix: the first voice/webhook turn worked, later ones stopped
    # committing orders.
    service = _LoopBoundSessionService()
    runner = build_runner(echo_agent, service)
    assert run_turn(runner, sender_id="+9000000001", message="hi") == "Echo: hi"
    assert run_turn(runner, sender_id="+9000000001", message="hi again") == ""


def test_turn_executor_runs_consecutive_turns_on_one_live_loop(echo_agent):
    # Regression: the executor keeps the session service on a loop that stays
    # open, so any number of consecutive turns (webhook posts, voice calls,
    # roundtrips) succeed — the second one used to fail with "Event loop is
    # closed".
    service = _LoopBoundSessionService()
    executor = TurnExecutor(echo_agent, service)
    try:
        for message in ["hi", "again", "and again"]:
            assert executor.run_turn(sender_id="+9000000001", message=message) == (
                f"Echo: {message}"
            )
        assert not service._bound_loop.is_closed()
        # Every turn ran on exactly one loop — the executor's persistent one.
        assert len({id(loop) for loop in service.touched_loops}) == 1
    finally:
        executor.stop()


def test_turn_executor_continues_durable_session(echo_agent):
    # The durable-session seam (ADR-0001): consecutive messages from the same
    # sender continue the same session on the executor path, so a clarifying
    # conversation survives across webhook calls.
    service = InMemorySessionService()
    executor = TurnExecutor(echo_agent, service)
    try:
        executor.run_turn(sender_id="+919812345001", message="hello")
        executor.run_turn(sender_id="+919812345001", message="still there")
        sessions = asyncio.run(
            service.list_sessions(
                app_name=settings.app_name, user_id="+919812345001"
            )
        )
        assert len(sessions.sessions) == 1
    finally:
        executor.stop()
