"""Catalogue and config administration through the AdminService seam (issue #58)."""

from __future__ import annotations

import pytest

from src.admin import AdminService, CatalogValidationError, ConfigValidationError
from src.store import InMemoryOrderStore


@pytest.fixture
def service():
    return AdminService(InMemoryOrderStore())


async def test_list_catalog_reads_all_five_kinds(service):
    for kind in (
        "customers",
        "products",
        "routes",
        "delivery_locations",
        "approvers",
    ):
        items = await service.list_catalog(kind)
        assert items
        assert all("id" in item for item in items)


async def test_unknown_catalog_kind_is_rejected(service):
    with pytest.raises(CatalogValidationError, match="unknown catalog kind"):
        await service.list_catalog("orders")


async def test_upsert_validates_required_fields_and_types(service):
    with pytest.raises(CatalogValidationError, match="missing"):
        await service.upsert_catalog("customers", [{"id": "c_incomplete"}])

    with pytest.raises(CatalogValidationError, match="phone"):
        await service.upsert_catalog(
            "customers",
            [
                {
                    "id": "c_bad",
                    "name": "Bad",
                    "phone": 42,
                    "gstin": "",
                    "state": "",
                    "ledger": "",
                }
            ],
        )

    with pytest.raises(CatalogValidationError, match="unexpected"):
        await service.upsert_catalog("routes", [{"id": "r", "name": "R", "oops": 1}])


async def test_upsert_merges_by_id_and_preserves_other_items(service):
    original = await service.list_catalog("customers")
    first = dict(original[0])
    first["name"] = "Renamed Customer"
    new = {
        "id": "c_new",
        "name": "New Customer",
        "phone": "+919812345099",
        "gstin": "29ABCDE1234F9Z9",
        "state": "Karnataka",
        "ledger": "NEW CUSTOMER",
    }

    upserted = await service.upsert_catalog("customers", [first, new])
    after = await service.list_catalog("customers")

    assert upserted == 2
    assert len(after) == len(original) + 1
    assert next(item for item in after if item["id"] == first["id"])["name"] == (
        "Renamed Customer"
    )
    assert any(item["id"] == new["id"] for item in after)
    assert [item["id"] for item in after[: len(original)]] == [
        item["id"] for item in original
    ]


async def test_update_config_rejects_unknown_keys_and_bad_values(service):
    with pytest.raises(ConfigValidationError, match="unknown config key"):
        await service.update_config({"not_a_setting": 1})

    with pytest.raises(ConfigValidationError, match="value_cap_inr"):
        await service.update_config({"value_cap_inr": "lots"})

    with pytest.raises(ConfigValidationError, match="cutoff_time"):
        await service.update_config({"cutoff_time": "25:99"})

    with pytest.raises(ConfigValidationError, match="business_timezone"):
        await service.update_config({"business_timezone": "Mars/Olympus"})


async def test_update_config_takes_effect_on_the_next_read(service):
    before = await service.get_config()
    updated = await service.update_config(
        {
            "value_cap_inr": 250000,
            "cutoff_time": "19:45",
            "business_timezone": "Asia/Kolkata",
            "gst_igst_ledger": "Output IGST 28%",
        }
    )

    assert updated["value_cap_inr"] == 250000
    assert updated["cutoff_time"] == "19:45"
    after = await service.get_config()
    assert after["value_cap_inr"] == 250000
    assert after["gst_igst_ledger"] == "Output IGST 28%"
    assert after["min_confidence"] == before["min_confidence"]
