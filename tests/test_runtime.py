"""Agent-runtime seam and composition root (issue #54).

The web layer drives every turn through one AgentRuntime instead of building
ADK plumbing itself. Production and tests build that runtime through the same
composition function, with adapters either selected from configuration or
injected as in-memory doubles.
"""

from __future__ import annotations

import base64
import inspect
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from src.config import settings
from src.model_provider import FakeModelProvider, ModelCapabilities
from src.runtime import build_runtime
from src.store import InMemoryOrderStore
from src.voucher import InMemoryVoucherStore
from src.web import create_app
from src.whatsapp import MockWhatsAppSender

from .fakes import FakeRuntime

ROOT = Path(__file__).resolve().parents[1]


def _roundtrip(client: TestClient, message: str = "hello"):
    return client.post(
        "/api/roundtrip",
        json={"sender_id": "+919812345001", "message": message},
        headers={"Authorization": "Bearer probe-token"},
    )


def test_roundtrip_drives_turns_through_an_injected_runtime():
    runtime = FakeRuntime(reply="from the fake runtime")
    client = TestClient(create_app(runtime=runtime, roundtrip_token="probe-token"))

    response = _roundtrip(client, "two drums of acid")

    assert response.status_code == 200
    assert response.json()["reply"] == "from the fake runtime"
    assert runtime.turns == [("+919812345001", "two drums of acid", None)]


def test_runtime_capabilities_gate_voice_intake_before_a_turn():
    runtime = FakeRuntime(capabilities=ModelCapabilities(audio=False))
    client = TestClient(create_app(runtime=runtime, voice_ingest_token="ingest"))

    response = client.post(
        "/api/voice/ingest",
        json={
            "caller": "+919812345001",
            "audio_base64": base64.b64encode(b"audio").decode(),
            "mime_type": "audio/wav",
        },
        headers={"Authorization": "Bearer ingest"},
    )

    assert response.status_code == 503
    assert runtime.turns == []


def test_build_runtime_composes_injected_in_memory_adapters():
    store = InMemoryOrderStore()
    voucher_storage = InMemoryVoucherStore()
    provider = FakeModelProvider()
    sender = MockWhatsAppSender()

    runtime = build_runtime(
        store=store,
        voucher_storage=voucher_storage,
        model_provider=provider,
        whatsapp_sender=sender,
    )
    try:
        assert runtime.store is store
        assert runtime.voucher_storage is voucher_storage
        assert runtime.whatsapp_sender is sender
        assert runtime.capabilities == provider.capabilities
        assert (
            runtime.run_turn(sender_id="+919812345001", message="hello")
            == "Echo: hello"
        )
    finally:
        runtime.stop()


def test_build_runtime_selects_adapters_from_configuration():
    with (
        mock.patch.object(settings, "session_service", "memory"),
        mock.patch.object(settings, "order_store", "memory"),
        mock.patch.object(settings, "model_provider", "fake"),
    ):
        runtime = build_runtime()
    try:
        assert isinstance(runtime.store, InMemoryOrderStore)
        assert isinstance(runtime.voucher_storage, InMemoryVoucherStore)
        assert (
            runtime.run_turn(sender_id="+919812345001", message="hello")
            == "Echo: hello"
        )
    finally:
        runtime.stop()


def test_production_entry_point_uses_the_shared_composition_root():
    from src import main as main_module

    runtime = FakeRuntime(reply="composed by build_runtime")
    with (
        mock.patch.object(main_module, "build_runtime", return_value=runtime),
        mock.patch.object(settings, "roundtrip_token", "probe-token"),
    ):
        app = main_module.create_valence_app()

    response = _roundtrip(TestClient(app))
    assert response.json()["reply"] == "composed by build_runtime"
    assert runtime.turns


def test_generic_factories_have_no_firestore_adapter_assumptions():
    from src.agent_exec import TurnExecutor

    app_params = inspect.signature(create_app).parameters
    assert "firestore_client_factory" not in app_params
    assert "agent_session_factory" not in app_params
    assert "firestore_client_factory" not in inspect.signature(TurnExecutor).parameters
    assert "Firestore" not in inspect.getsource(create_app)
    assert "Firestore" not in inspect.getsource(TurnExecutor)


def test_runtime_and_domain_modules_stay_free_of_agent_framework_imports():
    modules = [
        "core",
        "orders",
        "store",
        "voucher",
        "loading",
        "money",
        "approval",
        "dispatch",
        "web",
        "review",
        "runtime",
    ]
    for name in modules:
        source = (ROOT / "src" / f"{name}.py").read_text(encoding="utf-8")
        assert "google.adk" not in source, name
        assert "google.genai" not in source, name
