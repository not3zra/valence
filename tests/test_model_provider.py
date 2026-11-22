"""Capability-aware model providers (issue #53, ADR-0008).

The provider seam decides which agent-runtime model runs and which intake
channels it can understand. Tests exercise the factory, the capability flags,
and the turn-level gate rather than any model vendor's SDK.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from subprocess import run
from sys import executable
from unittest import mock

import pytest
from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import Field

from src import agent as agent_module
from src.config import settings
from src.media import MediaObject
from src.model_provider import (
    FakeModelProvider,
    GeminiModelProvider,
    ModelCapabilities,
    build_model_provider,
)


class _MediaRecordingLlm(BaseLlm):
    """Records whether each model call saw inline media, then echoes text."""

    model: str = "fake-recorder"
    seen_media: list[bool] = Field(default_factory=list)

    def supported_models(self) -> list[str]:
        return ["fake-recorder.*"]

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        saw_media = any(
            part.inline_data is not None
            for content in llm_request.contents
            if content.role == "user"
            for part in content.parts
        )
        self.seen_media.append(saw_media)
        text = "".join(
            part.text or ""
            for content in llm_request.contents
            if content.role == "user"
            for part in content.parts
        )
        yield LlmResponse(
            content=types.Content(
                role="model", parts=[types.Part(text=f"Echo: {text}")]
            )
        )


def test_default_provider_is_native_gemini_with_every_capability():
    with mock.patch.object(settings, "gemini_model", "gemini-3.5-flash"):
        provider = build_model_provider()
    assert isinstance(provider, GeminiModelProvider)
    assert provider.model == "gemini-3.5-flash"
    assert provider.capabilities == ModelCapabilities(
        text=True, image=True, audio=True, response_schema=True
    )


def test_fake_provider_is_selected_by_configuration():
    with mock.patch.object(settings, "model_provider", "fake"):
        provider = build_model_provider()
    assert isinstance(provider, FakeModelProvider)
    assert isinstance(provider.model, BaseLlm)


def test_unknown_provider_name_is_rejected_with_an_actionable_error():
    with mock.patch.object(settings, "model_provider", "unknown"):
        with pytest.raises(ValueError, match="MODEL_PROVIDER"):
            build_model_provider()


def test_agent_runs_a_text_turn_with_an_injected_fake_provider():
    agent = agent_module.build_agent(provider=FakeModelProvider())
    runner = agent_module.build_runner(agent, InMemorySessionService())
    reply = agent_module.run_turn(
        runner, sender_id="+919812345001", message="hello"
    )
    assert reply == "Echo: hello"


def test_agent_uses_the_configured_provider_without_an_injected_model():
    with mock.patch.object(settings, "model_provider", "fake"):
        agent = agent_module.build_agent()
    assert isinstance(agent.model, BaseLlm)


def test_provider_without_image_capability_drops_photo_but_keeps_text():
    model = _MediaRecordingLlm()
    provider = FakeModelProvider(
        model=model, capabilities=ModelCapabilities(image=False)
    )
    agent = agent_module.build_agent(provider=provider)
    runner = agent_module.build_runner(agent, InMemorySessionService())

    reply = agent_module.run_turn(
        runner,
        sender_id="+919812345001",
        message="order text",
        media=MediaObject(data=b"fake-jpeg", mime_type="image/jpeg"),
        capabilities=provider.capabilities,
    )

    assert reply == "Echo: order text"
    assert model.seen_media and not any(model.seen_media)


def test_provider_without_audio_capability_drops_recording_but_keeps_text():
    model = _MediaRecordingLlm()
    provider = FakeModelProvider(
        model=model, capabilities=ModelCapabilities(audio=False)
    )
    agent = agent_module.build_agent(provider=provider)
    runner = agent_module.build_runner(agent, InMemorySessionService())

    reply = agent_module.run_turn(
        runner,
        sender_id="+919812345001",
        message="call nudge",
        media=MediaObject(data=b"fake-wav", mime_type="audio/wav"),
        capabilities=provider.capabilities,
    )

    assert reply == "Echo: call nudge"
    assert model.seen_media and not any(model.seen_media)


def test_full_capability_provider_still_attaches_media():
    model = _MediaRecordingLlm()
    provider = FakeModelProvider(model=model)
    agent = agent_module.build_agent(provider=provider)
    runner = agent_module.build_runner(agent, InMemorySessionService())

    agent_module.run_turn(
        runner,
        sender_id="+919812345001",
        message="photo",
        media=MediaObject(data=b"fake-jpeg", mime_type="image/jpeg"),
        capabilities=provider.capabilities,
    )

    assert model.seen_media and all(model.seen_media)


def test_litellm_is_not_a_declared_dependency():
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    assert "litellm" not in pyproject.lower()


def test_default_path_never_imports_litellm():
    block_litellm = """
import importlib.abc
import sys


class _BlockLiteLlm(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "litellm" or fullname.startswith("litellm."):
            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
        return None


sys.meta_path.insert(0, _BlockLiteLlm())
"""
    result = run(
        [
            executable,
            "-c",
            block_litellm
            + """
import src.agent
import src.main
import src.web
from src.model_provider import build_model_provider

provider = build_model_provider()
assert provider.model == "gemini-3.5-flash", provider.model
assert not provider.model.startswith("litellm/")
assert "litellm" not in sys.modules
""",
        ],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
