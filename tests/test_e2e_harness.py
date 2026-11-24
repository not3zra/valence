"""Real-process end-to-end harness for Portable Valence v1 (issue #93).

The harness drives only public surfaces — CLI subprocesses and real HTTP — so
this test asserts its one-command contract rather than any internal step.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "e2e_harness.py"


def _harness_module():
    spec = importlib.util.spec_from_file_location("e2e_harness", HARNESS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_contains_order_accepts_section_late_and_unrouted_entries():
    harness = _harness_module()
    render = {
        "sections": [{"entries": [{"order_id": "ord_section"}]}],
        "late": [{"order_id": "ord_late"}],
        "unrouted": [{"order_id": "ord_unrouted"}],
    }

    assert harness._contains_order(render, "ord_section")
    assert harness._contains_order(render, "ord_late")
    assert harness._contains_order(render, "ord_unrouted")
    assert not harness._contains_order(render, "ord_missing")


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
