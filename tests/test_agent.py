"""ADK agent + durable-session wiring (the deploy smoke-test seam).

Gemini is faked at the model boundary; the runner, session service and turn
dispatch are the real ADK code paths, so these tests pin the "message in ->
reply out" contract that ticket 1 accepts on first deploy.
"""

from __future__ import annotations

import asyncio
from unittest import mock

import pytest
from google.adk.agents import Agent
from google.adk.sessions import InMemorySessionService

from src import agent as agent_module
from src.agent import build_agent, build_runner, build_session_service, run_turn

from .fakes import FakeEchoLlm


@pytest.fixture
def runner():
    agent = build_agent(model=FakeEchoLlm())
    session_service = InMemorySessionService()
    return build_runner(agent, session_service), session_service


def test_message_in_reply_out(runner):
    runner_inst, _ = runner
    reply = run_turn(runner_inst, sender_id="+919812345001", message="hello")
    assert reply == "Echo: hello"


def test_same_sender_reuses_one_durable_session(runner):
    runner_inst, session_service = runner
    run_turn(runner_inst, sender_id="+919812345001", message="first")
    run_turn(runner_inst, sender_id="+919812345001", message="second")

    sessions = asyncio.run(
        session_service.list_sessions(app_name=agent_module.settings.app_name)
    )
    assert [s.id for s in sessions.sessions] == ["+919812345001"]


def test_different_senders_get_different_sessions(runner):
    runner_inst, session_service = runner
    run_turn(runner_inst, sender_id="+919812345001", message="a")
    run_turn(runner_inst, sender_id="+919812345002", message="b")

    sessions = asyncio.run(
        session_service.list_sessions(app_name=agent_module.settings.app_name)
    )
    assert {s.id for s in sessions.sessions} == {
        "+919812345001",
        "+919812345002",
    }


def test_agent_defaults_to_configured_gemini_model():
    with mock.patch.object(agent_module.settings, "gemini_model", "gemini-3.5-flash"):
        agent = build_agent()
    assert isinstance(agent, Agent)
    assert agent.model == "gemini-3.5-flash"


def test_session_service_factory_uses_firestore_by_default():
    mock_target = (
        "google.adk.integrations.firestore.firestore_session_service"
        ".FirestoreSessionService"
    )
    with mock.patch.object(
        agent_module.settings, "session_service", "firestore"
    ), mock.patch(mock_target) as firestore_service:
        build_session_service()
    call_kwargs = firestore_service.call_args
    assert call_kwargs[1]["root_collection"] == agent_module.settings.firestore_root_collection
    assert "client" in call_kwargs[1]


def test_session_service_factory_memory_override():
    with mock.patch.object(
        agent_module.settings, "session_service", "memory"
    ):
        service = build_session_service()
    assert isinstance(service, InMemorySessionService)
