"""Catalogue and runtime configuration admin for one Instance (issue #58).

The admin API and ``valence setup`` both go through this service: it validates
imports against the seed document shapes and writes through the same
``OrderStore`` the agent reads, so a config or catalogue change takes effect on
the next order without a code change or a restart.
"""

from __future__ import annotations

import types
from dataclasses import MISSING, asdict, fields
from datetime import time
from typing import Any, Union, get_args, get_origin, get_type_hints
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import seed_data
from .store import OrderStore

CATALOG_KINDS = tuple(seed_data.DOCUMENT_TYPES)


class CatalogValidationError(ValueError):
    """An imported document does not match its catalog collection."""


class ConfigValidationError(ValueError):
    """A runtime setting is unknown or has the wrong shape."""


def _matches(value: Any, annotation: Any) -> bool:
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        return any(_matches(value, argument) for argument in get_args(annotation))
    if annotation is Any:
        return True
    if origin is list:
        arguments = get_args(annotation)
        return isinstance(value, list) and (
            not arguments or all(_matches(item, arguments[0]) for item in value)
        )
    if origin is dict:
        arguments = get_args(annotation)
        return isinstance(value, dict) and (
            not arguments
            or all(
                _matches(key, arguments[0]) and _matches(item, arguments[1])
                for key, item in value.items()
            )
        )
    if annotation is str:
        return isinstance(value, str)
    if annotation is bool:
        return isinstance(value, bool)
    if annotation is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if annotation is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return True


def _check_kind(kind: str) -> type:
    try:
        return seed_data.document_type_for(kind)
    except ValueError as error:
        raise CatalogValidationError(str(error)) from error


def _validate_item(kind: str, item: Any) -> None:
    document_type = _check_kind(kind)
    if not isinstance(item, dict):
        raise CatalogValidationError(f"{kind} items must be objects")
    field_types = get_type_hints(document_type)
    for field in fields(document_type):
        if field.name not in item:
            if field.default is MISSING and field.default_factory is MISSING:
                raise CatalogValidationError(
                    f"{kind} item is missing required field {field.name!r}"
                )
        elif not _matches(item[field.name], field_types[field.name]):
            raise CatalogValidationError(
                f"{kind} field {field.name!r} has an invalid value"
            )
    for name in item:
        if name not in field_types:
            raise CatalogValidationError(
                f"{kind} item has unexpected field {name!r}"
            )


def _valid_time(value: str) -> bool:
    try:
        time.fromisoformat(value)
    except ValueError:
        return False
    return True


def _valid_zone(value: str) -> bool:
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError):
        return False
    return True


class AdminService:
    """Validate and apply catalogue imports and runtime config updates."""

    def __init__(self, store: OrderStore) -> None:
        self._store = store

    async def list_catalog(self, kind: str) -> list[dict[str, Any]]:
        _check_kind(kind)
        getter = getattr(self._store, f"get_{kind}")
        return [asdict(item) for item in await getter()]

    async def upsert_catalog(self, kind: str, items: list[dict[str, Any]]) -> int:
        _check_kind(kind)
        if not isinstance(items, list):
            raise CatalogValidationError("catalog items must be a list")
        for item in items:
            _validate_item(kind, item)
        await self._store.upsert_catalog(kind, items)
        return len(items)

    async def get_config(self) -> dict[str, Any]:
        return dict(await self._store.get_config())

    async def update_config(self, values: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(values, dict) or not values:
            raise ConfigValidationError("config must be a non-empty object")
        current = await self._store.get_config()
        validated: dict[str, Any] = {}
        for key, value in values.items():
            if key not in current:
                raise ConfigValidationError(f"unknown config key {key!r}")
            if key == "cutoff_time":
                if not isinstance(value, str) or not _valid_time(value):
                    raise ConfigValidationError("invalid cutoff_time")
            elif key == "business_timezone":
                if not isinstance(value, str) or not _valid_zone(value):
                    raise ConfigValidationError("invalid business_timezone")
            else:
                expected = type(current[key])
                if not _matches(value, expected):
                    raise ConfigValidationError(
                        f"config key {key!r} must be {expected.__name__}"
                    )
                if expected is float and isinstance(value, int):
                    value = float(value)
            validated[key] = value
        merged = dict(current)
        merged.update(validated)
        await self._store.set_config(validated)
        return merged
