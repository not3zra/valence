"""``valence cutoff`` renders the same Loading List the v1 API serves (issue #97)."""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from src.cli import main
from src.loading import load_loading_list
from src.orders import Order, OrderItem, OrderStatus
from src.sql_store import SqlOrderStore

DAY = "2026-08-14"
NEXT_DAY = "2026-08-15"


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'cutoff.db'}"


def _order(order_id: str, day: str) -> Order:
    when = f"{day}T10:00:00+00:00"
    return Order(
        order_id=order_id,
        phone="+919812345001",
        customer="ChemFab Industries",
        delivery_location="Peenya Industrial Area",
        delivery_location_id="dl_peenya",
        items=[OrderItem(product="Sulfuric Acid", quantity=2000, unit="kg")],
        status=OrderStatus.APPROVED,
        created_at=when,
        updated_at=when,
    )


def _seed(url: str, *days: str) -> None:
    async def seed() -> None:
        store = SqlOrderStore(url)
        await store.initialize()
        for day in days:
            await store.create_order(_order(f"ord_{day}", day))
        await store.engine.dispose()

    asyncio.run(seed())


def test_cutoff_renders_a_day_as_json_from_the_configured_database(tmp_path, capsys):
    url = _sqlite_url(tmp_path)
    _seed(url, DAY)

    code = main(["cutoff", "--database-url", url, "--day", DAY])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)

    async def expected_list() -> dict:
        store = SqlOrderStore(url)
        try:
            loading = await load_loading_list(
                store, delivery_day=date.fromisoformat(DAY)
            )
            return loading.to_dict()
        finally:
            await store.engine.dispose()

    assert payload == asyncio.run(expected_list())
    entries = [entry for section in payload["sections"] for entry in section["entries"]]
    assert {entry["order_id"] for entry in entries} == {f"ord_{DAY}"}


def test_cutoff_renders_a_window_as_one_list_per_day(tmp_path, capsys):
    url = _sqlite_url(tmp_path)
    _seed(url, DAY, NEXT_DAY)

    code = main(
        [
            "cutoff",
            "--database-url",
            url,
            "--start",
            DAY,
            "--end",
            NEXT_DAY,
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert [item["delivery_day"] for item in payload] == [DAY, NEXT_DAY]


def test_cutoff_writes_the_render_to_a_file(tmp_path, capsys):
    url = _sqlite_url(tmp_path)
    _seed(url, DAY)
    output = tmp_path / "loading-list.json"

    code = main(
        ["cutoff", "--database-url", url, "--day", DAY, "--output", str(output)]
    )

    assert code == 0
    assert capsys.readouterr().out == ""
    assert json.loads(output.read_text())["delivery_day"] == DAY


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--day", DAY, "--start", DAY, "--end", NEXT_DAY], "either day or window"),
        (["--day", "not-a-date"], "invalid day"),
        (["--start", NEXT_DAY, "--end", DAY], "end must not precede start"),
        (
            [
                "--start",
                DAY,
                "--end",
                (date(2026, 8, 14) + timedelta(days=31)).isoformat(),
            ],
            "window is too large",
        ),
    ],
)
def test_cutoff_reports_clear_validation_errors(tmp_path, args, message):
    url = _sqlite_url(tmp_path)
    with pytest.raises(SystemExit, match=message):
        main(["cutoff", "--database-url", url, *args])


def test_cutoff_rejects_an_unsupported_database_url(tmp_path):
    with pytest.raises(SystemExit, match="unsupported DATABASE_URL"):
        main(["cutoff", "--database-url", "firestore://demo", "--day", DAY])


def test_cutoff_help_documents_date_window_and_output(tmp_path, capsys):
    with pytest.raises(SystemExit) as error:
        main(["cutoff", "--help"])

    assert error.value.code == 0
    help_text = capsys.readouterr().out
    assert "--day" in help_text
    assert "--start" in help_text and "--end" in help_text
    assert "--output" in help_text
    assert "--database-url" in help_text


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_cutoff_works_on_both_sql_backends(tmp_path, capsys, backend):
    url = _sqlite_url(tmp_path)
    if backend == "postgres":
        postgres_url = __import__("os").environ.get("TEST_POSTGRES_URL", "")
        if not postgres_url:
            pytest.skip("set TEST_POSTGRES_URL to run Postgres conformance")
        url = postgres_url
    _seed(url, DAY)

    code = main(["cutoff", "--database-url", url, "--day", DAY])

    assert code == 0
    assert json.loads(capsys.readouterr().out)["delivery_day"] == DAY
