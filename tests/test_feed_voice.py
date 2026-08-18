"""The company-recorded call feed script (issue #35).

The CLI reads a folder of recordings (caller from a ``<name>.caller`` sidecar)
or a Cloud Storage bucket (caller from object metadata) and POSTs each to the
token-gated ``/api/voice/ingest`` endpoint. These tests pin the folder and
bucket enumeration, the feed-to-endpoint round trip against the committed
sample recording (CI-runnable with a fake model), and the caller-is-metadata
rule.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from google.adk.sessions import InMemorySessionService

from scripts import feed_voice
from scripts.feed_voice import Recording, feed, folder_recordings
from src.agent import build_agent
from src.media import MAX_MEDIA_BYTES
from src.store import InMemoryOrderStore
from src.web import create_app

from .fakes import VoiceReadingLlm

INGEST_URL = "/api/voice/ingest"
TOKEN = "ingest-token"
CALLER = "+919812345001"

# The committed sample recording the CI run feeds through the real HTTP path.
SAMPLE_RECORDING = (
    Path(__file__).resolve().parents[1] / "scripts" / "eval_cases" / "order_call.wav"
)


def _app(store: InMemoryOrderStore):
    return create_app(
        agent=build_agent(model=VoiceReadingLlm(), store=store),
        session_service=InMemorySessionService(),
        voice_ingest_token=TOKEN,
    )


def test_audio_mime_from_name_maps_audio_extensions():
    assert feed_voice.audio_mime_for_name("call.wav") == "audio/wav"
    assert feed_voice.audio_mime_for_name("call.mp3") == "audio/mpeg"
    assert feed_voice.audio_mime_for_name("call.m4a") == "audio/mp4"
    assert feed_voice.audio_mime_for_name("call.amr") == "audio/amr"
    assert feed_voice.audio_mime_for_name("call.ogg") == "audio/ogg"
    assert feed_voice.audio_mime_for_name("notes.txt") is None
    assert feed_voice.audio_mime_for_name("order_call.WAV") == "audio/wav"


def test_folder_recordings_reads_sidecars_and_skips_the_rest(tmp_path):
    (tmp_path / "call.wav").write_bytes(b"RIFF fake-wav")
    (tmp_path / "call.caller").write_text("+919812345001")
    (tmp_path / "no_sidecar.wav").write_bytes(b"RIFF fake-wav")
    (tmp_path / "bad_sidecar.wav").write_bytes(b"RIFF fake-wav")
    (tmp_path / "bad_sidecar.caller").write_text("not-a-phone")
    (tmp_path / "notes.txt").write_text("not audio")

    recordings = folder_recordings(tmp_path)
    assert [(r.name, r.caller) for r in recordings] == [
        ("call.wav", "+919812345001")
    ]
    assert recordings[0].mime_type == "audio/wav"
    assert recordings[0].data == b"RIFF fake-wav"


def test_feed_reports_statuses_for_each_recording():
    store = InMemoryOrderStore()
    app = _app(store)

    async def _run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            return await feed(
                client,
                INGEST_URL,
                TOKEN,
                [Recording(CALLER, b"RIFF fake-wav", "audio/wav", "call.wav")],
            )

    results = asyncio.run(_run())
    assert results[0].ok
    assert results[0].detail == "HTTP 200"
    assert len(store.orders) == 1


@pytest.mark.asyncio
async def test_feed_commits_the_committed_sample_recording(tmp_path):
    """CI-runnable feed: the committed sample wav becomes a voice order."""
    store = InMemoryOrderStore()
    app = _app(store)
    (tmp_path / "order_call.wav").write_bytes(SAMPLE_RECORDING.read_bytes())
    (tmp_path / "order_call.caller").write_text(CALLER)

    recordings = folder_recordings(tmp_path)
    assert len(recordings) == 1

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        results = await feed(client, INGEST_URL, TOKEN, recordings)

    assert [r.ok for r in results] == [True]
    assert len(store.orders) == 1
    order = store.orders[0]
    assert order.phone == CALLER
    assert order.source_channel == "voice"
    assert order.status.value == "approved"


@pytest.mark.asyncio
async def test_feed_reports_a_wrong_token(tmp_path):
    store = InMemoryOrderStore()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(store)),
        base_url="http://testserver",
    ) as client:
        results = await feed(
            client,
            INGEST_URL,
            "wrong-token",
            [Recording(CALLER, b"RIFF fake-wav", "audio/wav", "call.wav")],
        )
    assert results[0].ok is False
    assert "401" in results[0].detail
    assert store.orders == []


@pytest.mark.asyncio
async def test_feed_skips_an_oversized_recording_local():
    async with httpx.AsyncClient() as client:
        results = await feed(
            client,
            INGEST_URL,
            TOKEN,
            [
                Recording(
                    CALLER,
                    b"x" * (MAX_MEDIA_BYTES + 1),
                    "audio/wav",
                    "big.wav",
                )
            ],
        )
    assert results[0].ok is False
    assert "cap" in results[0].detail


class _FakeBlob:
    def __init__(self, name, metadata=None, data=b"RIFF fake-wav"):
        self.name = name
        self.metadata = metadata
        self._data = data

    def download_as_bytes(self):
        return self._data


class _FakeBucket:
    def __init__(self, blobs):
        self._blobs = blobs

    def list_blobs(self, prefix=""):
        return [blob for blob in self._blobs if blob.name.startswith(prefix)]


class _FakeStorageClient:
    def __init__(self, bucket):
        self._bucket = bucket

    def bucket(self, name):
        return self._bucket


def test_bucket_recordings_reads_caller_from_metadata():
    client = _FakeStorageClient(
        _FakeBucket(
            [
                _FakeBlob("calls/a.wav", metadata={"caller": "+919812345001"}),
                _FakeBlob("calls/b.wav"),  # no caller metadata -> skipped
                _FakeBlob(
                    "calls/c.wav", metadata={"caller": "not-a-phone"}
                ),  # invalid -> skipped
                _FakeBlob("calls/notes.txt"),  # not audio -> skipped
            ]
        )
    )
    recordings = feed_voice.bucket_recordings(
        "valence-calls", prefix="calls/", storage_client=client
    )
    assert [(r.name, r.caller) for r in recordings] == [
        ("calls/a.wav", "+919812345001")
    ]
    assert recordings[0].data == b"RIFF fake-wav"


@pytest.mark.asyncio
async def test_bucket_feed_path_commits_orders():
    store = InMemoryOrderStore()
    storage = _FakeStorageClient(
        _FakeBucket(
            [
                _FakeBlob(
                    "day/a.wav",
                    metadata={"caller": "+919812345001"},
                    data=b"RIFF fake-wav",
                ),
                _FakeBlob(
                    "day/b.wav",
                    metadata={"caller": "+919812345002"},
                    data=b"RIFF fake-wav",
                ),
            ]
        )
    )
    recordings = feed_voice.bucket_recordings("valence-calls", storage_client=storage)
    assert len(recordings) == 2

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(store)),
        base_url="http://testserver",
    ) as client:
        results = await feed(client, INGEST_URL, TOKEN, recordings)

    assert [r.ok for r in results] == [True, True]
    phones = sorted(order.phone for order in store.orders)
    assert phones == ["+919812345001", "+919812345002"]
    assert all(order.source_channel == "voice" for order in store.orders)


def test_main_requires_a_token(monkeypatch):
    monkeypatch.delenv("VOICE_INGEST_TOKEN", raising=False)
    assert feed_voice.main(["--folder", "."]) == 2
