"""Real-process end-to-end harness for Portable Valence v1 (issue #93).

The harness drives only public surfaces — CLI subprocesses and real HTTP — so
this test asserts its one-command contract rather than any internal step.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "e2e_harness.py"


def test_harness_runs_a_clean_sqlite_flow_and_reports_every_step(tmp_path):
    artifacts = tmp_path / "artifacts"
    result = subprocess.run(
        [sys.executable, str(HARNESS), "--artifacts", str(artifacts)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=300,
    )

    assert result.returncode == 0, (
        f"harness failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    report = json.loads((artifacts / "report.json").read_text())
    assert report["status"] == "pass"
    names = {step["name"] for step in report["steps"]}
    assert {
        "init",
        "setup",
        "intake",
        "approve",
        "loading lists",
        "whatsapp webhook",
        "push to tally",
        "restart",
        "persistence",
    } <= names
    assert all(step["status"] == "pass" for step in report["steps"])
    assert (artifacts / "voucher.xml").read_text().startswith("<")
