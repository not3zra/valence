"""``valence setup``: an HTTP wrapper over the admin API (issue #58, ADR-0009).

After ``valence init`` issues the first admin key, configuration belongs to the
API. This module loads a sample fixture from disk and applies it through the
same authenticated routes an agent or the web UI uses, so there is a single
write path for catalogue, config, and Integration credentials.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol
from urllib import error, request

from .admin import CATALOG_KINDS
from .seed_data import document_type_for


class SetupError(RuntimeError):
    """The fixture or the admin API rejected an onboarding step."""


class SetupClient(Protocol):
    def request(self, method: str, path: str, payload: Any = None) -> Any: ...


class HttpSetupClient:
    """The production ``SetupClient``: bearer-authenticated JSON over HTTP."""

    def __init__(self, base_url: str, admin_key: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._admin_key = admin_key

    def request(self, method: str, path: str, payload: Any = None) -> Any:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        http_request = request.Request(
            f"{self._base_url}{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self._admin_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with request.urlopen(http_request, timeout=30) as response:
                raw = response.read()
        except error.HTTPError as failure:
            try:
                detail = failure.read().decode("utf-8", errors="replace")[:200]
            except (OSError, ValueError):
                detail = ""
            raise SetupError(
                f"{method} {path} failed ({failure.code}): {detail}"
            ) from failure
        except error.URLError as failure:
            raise SetupError(f"{method} {path} failed: {failure.reason}") from failure
        return json.loads(raw) if raw else None


@dataclass(frozen=True)
class Fixture:
    business_name: str
    timezone: str
    catalog: dict[str, list[dict[str, Any]]]
    config: dict[str, Any]
    integrations: dict[str, dict[str, str]] = field(default_factory=dict)


def load_fixture(directory: str | Path) -> Fixture:
    """Read a sample fixture directory; secrets, if any, live in a separate file."""
    root = Path(directory)
    try:
        profile = json.loads((root / "instance.json").read_text(encoding="utf-8"))
        catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
        config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    except OSError as failure:
        raise SetupError(f"cannot read fixture {root}: {failure}") from failure
    except json.JSONDecodeError as failure:
        raise SetupError(f"invalid fixture JSON in {root}: {failure}") from failure
    _check_catalog_kinds(catalog)
    integrations: dict[str, dict[str, str]] = {}
    integration_file = root / "integrations.json"
    if integration_file.exists():
        integrations = json.loads(integration_file.read_text(encoding="utf-8"))
    return Fixture(
        business_name=profile["business_name"],
        timezone=profile.get("timezone", "UTC"),
        catalog=catalog,
        config=config,
        integrations=integrations,
    )


def _check_catalog_kinds(catalog: Mapping[str, object]) -> None:
    for kind in catalog:
        try:
            document_type_for(kind)
        except ValueError as error:
            raise SetupError(str(error)) from error


def with_integrations(fixture: Fixture, path: str | Path) -> Fixture:
    """Attach a separate, deployer-supplied credentials file to a fixture."""
    values = json.loads(Path(path).read_text(encoding="utf-8"))
    return replace(fixture, integrations=values)


def run_setup(client: SetupClient, fixture: Fixture) -> list[str]:
    """Apply one fixture in dependency order; return a human-readable step list."""
    _check_catalog_kinds(fixture.catalog)
    steps: list[str] = []
    client.request(
        "PATCH",
        "/api/v1/instance",
        {
            "business_name": fixture.business_name,
            "timezone": fixture.timezone,
            "config": fixture.config,
        },
    )
    steps.append("instance profile and config")
    for kind in CATALOG_KINDS:
        items = fixture.catalog.get(kind, [])
        if not items:
            continue
        client.request("PUT", f"/api/v1/catalog/{kind}", items)
        steps.append(f"{kind}: {len(items)}")
    for service, values in fixture.integrations.items():
        client.request("PUT", f"/api/v1/integrations/{service}", values)
        steps.append(f"integration {service}")
    return steps
