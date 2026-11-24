"""Feed a batch of company-recorded calls into Agent Interface voice intake.

The company records its own sales calls and feeds them here, one order per
recording (issue #35). Each recording's caller number is read from trusted
company metadata — a ``<name>.caller`` sidecar next to a folder recording, or
the blob's ``caller`` metadata in Cloud Storage — never guessed and never
spoofable from outside the authenticated path. Each recording is POSTed to
``/api/v1/intake/voice``, which runs it through the same ADK agent turn as the
other channels and commits a voice order (a missing field escalates per
ADR-0004). The route requires an intake-role API key, passed with ``--api-key``
or the ``VALENCE_API_KEY`` environment variable.

Usage:
    python scripts/feed_voice.py --folder path/to/calls
    python scripts/feed_voice.py --folder path/to/calls --api-key VALENCE_KEY
    python scripts/feed_voice.py --bucket my-bucket --prefix calls/2026-08-18/
    python scripts/feed_voice.py --bucket my-bucket --endpoint https://<service>/api/v1/intake/voice

A folder recording is ingested only when a ``<name>.caller`` sidecar next to it
holds the caller's E.164 number:
    path/to/calls/order_call.wav
    path/to/calls/order_call.caller        # +919812345001

A bucket recording is ingested only when the object's custom metadata carries a
``caller`` entry with the E.164 number.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from src.intake import E164_PATTERN
from src.media import MAX_MEDIA_BYTES, audio_mime_for_name

DEFAULT_ENDPOINT = "http://localhost:8080/api/v1/intake/voice"


@dataclass(frozen=True)
class Recording:
    """One recorded call ready to ingest: caller, audio bytes, mime."""

    caller: str
    data: bytes
    mime_type: str
    name: str


@dataclass
class FeedResult:
    """The outcome of feeding one recording."""

    name: str
    ok: bool
    detail: str = ""


def is_e164(value: str) -> bool:
    """Whole-string E.164 check, matching the ingest endpoint's validation."""
    return re.fullmatch(E164_PATTERN, value) is not None


def folder_recordings(folder: Path) -> list[Recording]:
    """Read every audio file in ``folder`` with a ``<name>.caller`` sidecar.

    A recording without a sidecar, or with a sidecar that is not a valid E.164
    number, is skipped with a warning — the caller is trusted company metadata
    and is never guessed.
    """
    recordings: list[Recording] = []
    for path in sorted(folder.iterdir()):
        if not path.is_file():
            continue
        mime = audio_mime_for_name(path.name)
        if mime is None:
            continue
        sidecar = path.with_suffix(".caller")
        if not sidecar.is_file():
            print(
                f"skipping {path.name}: no {sidecar.name} sidecar with the caller",
                file=sys.stderr,
            )
            continue
        caller = sidecar.read_text(encoding="utf-8").strip()
        if not is_e164(caller):
            print(
                f"skipping {path.name}: caller {caller!r} is not an E.164 number",
                file=sys.stderr,
            )
            continue
        recordings.append(
            Recording(
                caller=caller,
                data=path.read_bytes(),
                mime_type=mime,
                name=path.name,
            )
        )
    return recordings


def bucket_recordings(
    bucket_name: str, prefix: str = "", storage_client=None
) -> list[Recording]:
    """Read every audio blob in ``bucket_name`` with a ``caller`` metadata entry.

    ``storage_client`` defaults to a real ``google.cloud.storage.Client`` and is
    injectable for tests. The caller comes from the blob's custom metadata —
    the company's recording system supplies it — never from the blob name.
    """
    client = storage_client or _default_storage_client()
    recordings: list[Recording] = []
    for blob in client.bucket(bucket_name).list_blobs(prefix=prefix):
        mime = audio_mime_for_name(blob.name)
        if mime is None:
            continue
        caller = (blob.metadata or {}).get("caller")
        if not caller or not is_e164(caller):
            print(
                f"skipping {blob.name}: no caller metadata (or not E.164)",
                file=sys.stderr,
            )
            continue
        recordings.append(
            Recording(
                caller=caller,
                data=blob.download_as_bytes(),
                mime_type=mime,
                name=blob.name,
            )
        )
    return recordings


def _default_storage_client():
    from google.cloud import storage

    return storage.Client()


async def feed(
    client: httpx.AsyncClient,
    endpoint: str,
    api_key: str,
    recordings: list[Recording],
) -> list[FeedResult]:
    """POST each recording to the v1 intake endpoint and report the outcomes."""
    results: list[FeedResult] = []
    for recording in recordings:
        if len(recording.data) > MAX_MEDIA_BYTES:
            results.append(
                FeedResult(
                    name=recording.name,
                    ok=False,
                    detail="audio over the 5 MiB cap",
                )
            )
            continue
        payload = {
            "caller": recording.caller,
            "audio_base64": base64.b64encode(recording.data).decode(),
            "mime_type": recording.mime_type,
        }
        try:
            response = await client.post(
                endpoint,
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
            )
        except httpx.HTTPError as exc:
            results.append(
                FeedResult(name=recording.name, ok=False, detail=str(exc))
            )
            continue
        results.append(
            FeedResult(
                name=recording.name,
                ok=response.status_code == 200,
                detail=f"HTTP {response.status_code}",
            )
        )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Feed company-recorded calls into Agent Interface voice intake, "
            "one order per recording (issue #35)."
        )
    )
    parser.add_argument(
        "--folder",
        type=Path,
        help="folder of recordings; caller read from a <name>.caller sidecar each",
    )
    parser.add_argument(
        "--bucket",
        help="Cloud Storage bucket of recordings; caller read from object metadata",
    )
    parser.add_argument(
        "--prefix",
        default="",
        help="object-name prefix within --bucket (e.g. calls/2026-08-18/)",
    )
    parser.add_argument(
        "--endpoint",
        default=DEFAULT_ENDPOINT,
        help=f"voice intake endpoint URL (default: {DEFAULT_ENDPOINT})",
    )
    parser.add_argument(
        "--api-key",
        default="",
        help="intake-role API key (default: VALENCE_API_KEY env)",
    )
    parser.add_argument(
        "--timeout", type=float, default=60.0, help="HTTP timeout in seconds"
    )
    args = parser.parse_args(argv)

    api_key = args.api_key or os.environ.get("VALENCE_API_KEY", "")
    if not api_key:
        print(
            "no API key: pass --api-key or set VALENCE_API_KEY",
            file=sys.stderr,
        )
        return 2
    if args.folder and args.bucket:
        print("pick one of --folder or --bucket, not both", file=sys.stderr)
        return 2
    if args.folder:
        recordings = folder_recordings(args.folder)
    elif args.bucket:
        recordings = bucket_recordings(args.bucket, prefix=args.prefix)
    else:
        print("one of --folder or --bucket is required", file=sys.stderr)
        return 2
    if not recordings:
        print("no recordings to feed", file=sys.stderr)
        return 1

    async def _run() -> list[FeedResult]:
        async with httpx.AsyncClient(timeout=args.timeout) as client:
            return await feed(client, args.endpoint, api_key, recordings)

    results = asyncio.run(_run())
    ok = sum(1 for result in results if result.ok)
    print(f"Fed {ok}/{len(results)} recordings to {args.endpoint}")
    for result in results:
        marker = "OK" if result.ok else "FAIL"
        print(f"  {marker}  {result.name}  ({result.detail})")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
