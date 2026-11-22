"""``valence setup`` imports the sample fixture over the admin API (issue #58)."""

from __future__ import annotations

import asyncio
import io
from dataclasses import replace
from pathlib import Path
from urllib.error import HTTPError

import pytest
from fastapi.testclient import TestClient

from src import seed_data
from src import setup as setup_api
from src.auth import AuthService, InMemoryAuthStore
from src.cli import main
from src.core import OrderProcessingCore
from src.orders import Order, OrderItem
from src.setup import (
    HttpSetupClient,
    SetupError,
    load_fixture,
    run_setup,
)
from src.store import InMemoryOrderStore
from src.web import create_app

from .fakes import FakeRuntime

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "examples" / "acme-chemicals"


class RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    def request(self, method: str, path: str, payload=None):
        self.calls.append((method, path, payload))
        return {}


class ApiClientAdapter:
    def __init__(self, client: TestClient) -> None:
        self.client = client

    def request(self, method: str, path: str, payload=None):
        response = self.client.request(method, path, json=payload)
        if response.status_code >= 400:
            raise SetupError(
                f"{method} {path}: {response.status_code} {response.text}"
            )
        return response.json()


def _client():
    store = InMemoryOrderStore(
        customers=[],
        products=[],
        routes=[],
        delivery_locations=[],
        approvers=[],
        config=dict(seed_data.CONFIG),
    )
    auth = AuthService(InMemoryAuthStore())
    token = asyncio.run(auth.issue_admin_key(name="admin")).token
    client = TestClient(
        create_app(runtime=FakeRuntime(store=store), auth=auth),
        headers={"Authorization": f"Bearer {token}"},
    )
    return ApiClientAdapter(client), store


def test_sample_fixture_is_complete_and_secret_free():
    fixture = load_fixture(FIXTURE_DIR)

    assert fixture.business_name
    assert set(fixture.catalog) == {
        "customers",
        "products",
        "routes",
        "delivery_locations",
        "approvers",
    }
    assert all(fixture.catalog.values())
    assert fixture.config["cutoff_time"] == seed_data.CONFIG["cutoff_time"]
    assert fixture.integrations == {}
    assert "secret" not in (FIXTURE_DIR / "instance.json").read_text().lower()


def test_fixture_imports_into_an_empty_instance_and_takes_a_first_order():
    client, store = _client()
    fixture = load_fixture(FIXTURE_DIR)

    run_setup(client, fixture)

    assert len(asyncio.run(store.get_customers())) == len(fixture.catalog["customers"])
    assert len(asyncio.run(store.get_products())) == len(fixture.catalog["products"])
    assert len(asyncio.run(store.get_approvers())) == len(
        fixture.catalog["approvers"]
    )
    assert (asyncio.run(store.get_config()))["business_timezone"] == (
        fixture.config["business_timezone"]
    )
    customer = fixture.catalog["customers"][0]
    product = fixture.catalog["products"][0]
    location = fixture.catalog["delivery_locations"][0]
    decision = asyncio.run(
        OrderProcessingCore(store).process(
            Order(
                phone=customer["phone"],
                customer=customer["name"],
                items=[
                    OrderItem(
                        product=product["name"],
                        quantity=100,
                        unit=product["unit"],
                    )
                ],
                delivery_location=location["name"],
                confidence=0.9,
            )
        )
    )
    assert decision.approved


def test_import_rejects_unknown_catalog_kinds():
    fixture = load_fixture(FIXTURE_DIR)

    with pytest.raises(SetupError, match="unknown catalog kind"):
        run_setup(
            RecordingClient(),
            replace(fixture, catalog={"invoices": [{"id": "x"}]}),
        )


def test_setup_cli_posts_the_fixture_over_http(monkeypatch):
    recorder = RecordingClient()
    monkeypatch.setattr("src.cli.HttpSetupClient", lambda url, key: recorder)

    code = main(
        [
            "setup",
            "--url",
            "http://valence.local:8080",
            "--admin-key",
            "vk_admin",
            "--fixture",
            str(FIXTURE_DIR),
        ]
    )

    assert code == 0
    assert recorder.calls[0][:2] == ("PATCH", "/api/v1/instance")
    assert [path for method, path, _ in recorder.calls if method == "PUT"] == [
        f"/api/v1/catalog/{kind}"
        for kind in (
            "customers",
            "products",
            "routes",
            "delivery_locations",
            "approvers",
        )
    ]
    assert all("vk_admin" not in str(call) for call in recorder.calls)


def test_http_client_sends_bearer_token_and_never_echoes_it(monkeypatch):
    seen: dict[str, str] = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(request, timeout):
        seen.update(request.header_items())
        return _Response()

    monkeypatch.setattr(setup_api.request, "urlopen", fake_urlopen)
    client = HttpSetupClient("http://valence.local/", "vk_secret")

    assert client.request("GET", "/api/v1/instance") == {"ok": True}
    assert seen["Authorization"] == "Bearer vk_secret"

    def failing(request, timeout):
        raise HTTPError(
            request.full_url,
            422,
            "bad",
            {},
            io.BytesIO(b'{"detail":"unknown config key"}'),
        )

    monkeypatch.setattr(setup_api.request, "urlopen", failing)
    with pytest.raises(SetupError) as error:
        client.request("PUT", "/api/v1/catalog/customers", [])
    assert "vk_secret" not in str(error.value)
    assert "unknown config key" in str(error.value)
