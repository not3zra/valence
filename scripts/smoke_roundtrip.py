"""Smoke test: run one agent round trip against the real Gemini model.

Exercise of the ticket 1 deploy acceptance: "the deployed agent's first
round-trip runs (message in -> reply out)". Requires ``GOOGLE_API_KEY``.

Usage:
    GOOGLE_API_KEY=... python scripts/smoke_roundtrip.py \\
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
