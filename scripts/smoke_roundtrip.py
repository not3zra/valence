"""Smoke test: run one agent round trip against the real Gemini model.

Exercise of the ticket 1 deploy acceptance: "the deployed agent's first
round-trip runs (message in -> reply out)". Gemini is served through Vertex AI,
so set the Vertex env vars (or rely on ``./scripts/run_local.sh`` which exports
them) and run ``gcloud auth application-default login`` once.

Usage:
    GOOGLE_GENAI_USE_VERTEXAI=true GOOGLE_CLOUD_PROJECT=valence-505412 \\
        GOOGLE_CLOUD_LOCATION=asia-southeast1 \\
        python scripts/smoke_roundtrip.py \\
        --sender +919812345001 --message "Namaste, 2 drums sulfuric acid chahiye"
"""

from __future__ import annotations

import argparse

from src.agent import build_agent, build_runner, build_session_service, run_turn


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sender", default="+919812345001")
    parser.add_argument("--message", default="Namaste, 2 drums sulfuric acid chahiye")
    args = parser.parse_args()

    runner = build_runner(build_agent(), build_session_service())
    reply = run_turn(runner, sender_id=args.sender, message=args.message)
    print(f"sender: {args.sender}")
    print(f"reply : {reply}")


if __name__ == "__main__":
    main()
