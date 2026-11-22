"""The default install must run without the optional GCP extra (issue #52).

These tests block ``google.cloud`` imports in a fresh interpreter, so they fail
if any default-path module imports Firestore or Cloud Storage at import time or
constructs a GCP client while building the application. The GCP adapters stay
available behind ``valence-order-agent[gcp]``; only the default path is pinned
here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_BLOCK_GCP = """
import sys
import importlib.abc


class _BlockGoogleCloud(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "google.cloud" or fullname.startswith("google.cloud."):
            raise ModuleNotFoundError(
                f"No module named {fullname!r}", name=fullname
            )
        return None


sys.meta_path.insert(0, _BlockGoogleCloud())
"""


def _run_without_gcp(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _BLOCK_GCP + "\n" + code],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_default_application_imports_and_runs_without_gcp_extra():
    result = _run_without_gcp(
        """
import src.main
from fastapi.testclient import TestClient

response = TestClient(src.main.app).get("/health")
assert response.status_code == 200, response.text
assert "google.cloud" not in sys.modules
"""
    )
    assert result.returncode == 0, result.stderr


def test_default_adapters_import_without_gcp_extra():
    result = _run_without_gcp(
        """
import src.agent
import src.agent_exec
import src.seed_firestore
import src.store
import src.voucher

from src.store import InMemoryOrderStore
from src.voucher import InMemoryVoucherStore, default_voucher_storage

assert isinstance(default_voucher_storage(), InMemoryVoucherStore)
assert InMemoryOrderStore().orders == []
assert "google.cloud" not in sys.modules
"""
    )
    assert result.returncode == 0, result.stderr


def test_gcp_adapters_explain_the_missing_extra():
    result = _run_without_gcp(
        """
from src.agent import build_session_service
from src.config import settings
from src.store import FirestoreOrderStore
from src.voucher import default_voucher_storage


def assert_needs_gcp(call):
    try:
        call()
    except RuntimeError as exc:
        assert "gcp" in str(exc).lower(), str(exc)
    else:
        raise AssertionError("expected a missing GCP extra error")


assert_needs_gcp(lambda: default_voucher_storage("a-bucket"))
assert_needs_gcp(lambda: FirestoreOrderStore().client)
settings.session_service = "firestore"
assert_needs_gcp(build_session_service)
"""
    )
    assert result.returncode == 0, result.stderr


def test_gcp_packages_are_an_optional_extra():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    core, extras = pyproject.split("[project.optional-dependencies]", 1)
    gcp = extras.split("dev =", 1)[0]

    assert "google-cloud-firestore" not in core
    assert "google-cloud-storage" not in core
    assert "google-cloud-firestore" in gcp
    assert "google-cloud-storage" in gcp
