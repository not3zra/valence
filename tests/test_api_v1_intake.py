"""REST v1 intake and order inspection (issue #59)."""

from __future__ import annotations

import asyncio
import base64
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from src.auth import AuthService, InMemoryAuthStore, Role
from src.core import OrderProcessingCore
from src.media import MAX_MEDIA_BYTES
from src.model_provider import ModelCapabilities
from src.orders import Order, OrderEvent, OrderItem, OrderStatus
from src.store import InMemoryOrderStore
from src.web import create_app

from .fakes import FakeRuntime

CUSTOMER_PHONE = "+919812345001"


class CommittingRuntime(FakeRuntime):
    """Runtime double whose turn commits a clean order through the real core."""

    def __init__(self, store, confidence: float = 0.9, **kwargs) -> None:
        super().__init__(store=store, **kwargs)
        self.confidence = confidence
        self.committed: list = []

    def run_turn(self, *, sender_id, message, media=None):
        self.turns.append((sender_id, message, media))
        source_channel = "whatsapp"
        if media is not None and media.mime_type.startswith("image/"):
            source_channel = "photo"
        elif media is not None and media.mime_type.startswith("audio/"):
            source_channel = "voice"
        decision = asyncio.run(
            OrderProcessingCore(self.store).process(
                Order(
                    phone=sender_id,
                    customer="ChemFab Industries",
                    items=[
                        OrderItem(product="sulfuric acid", quantity=2000, unit="kg")
                    ],
                    delivery_location="Peenya Industrial Area",
                    confidence=self.confidence,
                    source_channel=source_channel,
                )
            )
        )
        self.committed.append(decision)
        return "fake reply"


def _client(
    *,
    capabilities: ModelCapabilities | None = None,
    confidence: float = 0.9,
):
    store = InMemoryOrderStore()
    runtime = CommittingRuntime(
        store=store, capabilities=capabilities, confidence=confidence
    )

    async def approver_phones():
        return [approver.phone for approver in await store.get_approvers()]

    auth = AuthService(InMemoryAuthStore(), approver_phones=approver_phones)
    admin = asyncio.run(auth.issue_admin_key(name="admin")).token
    operator = asyncio.run(auth.issue_key(name="operator", role=Role.OPERATOR)).token
    viewer = asyncio.run(auth.issue_key(name="viewer", role=Role.VIEWER)).token
    client = TestClient(create_app(runtime=runtime, auth=auth))
    return client, store, runtime, admin, operator, viewer


def _headers(token: str):
    return {"Authorization": f"Bearer {token}"}


def test_operator_intakes_text_order_and_receives_reply_and_decision():
    client, store, runtime, admin, operator, viewer = _client()

    response = client.post(
        "/api/v1/intake",
        json={"sender_id": CUSTOMER_PHONE, "message": "2000 kg sulfuric acid"},
        headers=_headers(operator),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reply"] == "fake reply"
    decision = body["decision"]
    assert decision["approved"] is True
    assert decision["status"] == "approved"
    assert decision["order_id"].startswith("ord_")
    assert body["order_id"] == decision["order_id"]
    assert len(store.orders) == 1
    assert runtime.turns[0][0] == CUSTOMER_PHONE
    assert runtime.turns[0][1] == "2000 kg sulfuric acid"


def test_intake_requires_authentication_and_an_intake_role():
    client, store, runtime, admin, operator, viewer = _client()
    body = {"sender_id": CUSTOMER_PHONE, "message": "2000 kg sulfuric acid"}

    assert client.post("/api/v1/intake", json=body).status_code == 401
    assert (
        client.post("/api/v1/intake", json=body, headers=_headers(viewer)).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/intake", json=body, headers=_headers(admin)
        ).status_code
        == 200
    )


def test_idempotency_key_returns_the_first_result_without_a_second_order():
    client, store, runtime, admin, operator, viewer = _client()
    body = {"sender_id": CUSTOMER_PHONE, "message": "2000 kg sulfuric acid"}
    headers = {**_headers(operator), "Idempotency-Key": "retry-1"}

    first = client.post("/api/v1/intake", json=body, headers=headers)
    second = client.post("/api/v1/intake", json=body, headers=headers)

    assert first.status_code == second.status_code == 200
    assert first.json()["order_id"] == second.json()["order_id"]
    assert first.json()["idempotent"] is False
    assert second.json()["idempotent"] is True
    assert len(store.orders) == 1
    assert len(runtime.turns) == 1


def test_reusing_a_key_with_a_different_payload_is_rejected():
    client, store, runtime, admin, operator, viewer = _client()
    headers = {**_headers(operator), "Idempotency-Key": "retry-1"}

    assert (
        client.post(
            "/api/v1/intake",
            json={"sender_id": CUSTOMER_PHONE, "message": "first message"},
            headers=headers,
        ).status_code
        == 200
    )
    conflict = client.post(
        "/api/v1/intake",
        json={"sender_id": CUSTOMER_PHONE, "message": "different message"},
        headers=headers,
    )

    assert conflict.status_code == 409
    assert len(store.orders) == 1


def test_voice_intake_commits_a_voice_order_through_the_same_pipeline():
    client, store, runtime, admin, operator, viewer = _client()
    audio = base64.b64encode(b"fake-wav-bytes").decode()

    response = client.post(
        "/api/v1/intake/voice",
        json={"caller": CUSTOMER_PHONE, "audio_base64": audio},
        headers=_headers(operator),
    )

    assert response.status_code == 200, response.text
    assert store.orders[0].source_channel == "voice"
    sender_id, message, media = runtime.turns[0]
    assert sender_id == CUSTOMER_PHONE
    assert media is not None and media.mime_type == "audio/wav"
    assert "recording of a phone call" in message


def test_voice_intake_is_closed_when_the_provider_has_no_audio_support():
    client, store, runtime, admin, operator, viewer = _client(
        capabilities=ModelCapabilities(audio=False)
    )
    audio = base64.b64encode(b"fake-wav-bytes").decode()

    response = client.post(
        "/api/v1/intake/voice",
        json={"caller": CUSTOMER_PHONE, "audio_base64": audio},
        headers=_headers(operator),
    )

    assert response.status_code == 503
    assert store.orders == []
    assert runtime.turns == []


def test_photo_intake_commits_a_photo_order_when_the_provider_sees_images():
    client, store, runtime, admin, operator, viewer = _client()
    image = base64.b64encode(b"fake-jpeg-bytes").decode()

    response = client.post(
        "/api/v1/intake/photo",
        json={
            "sender_id": CUSTOMER_PHONE,
            "image_base64": image,
            "mime_type": "image/png",
        },
        headers=_headers(operator),
    )

    assert response.status_code == 200, response.text
    assert store.orders[0].source_channel == "photo"
    media = runtime.turns[0][2]
    assert media is not None and media.mime_type == "image/png"


def test_photo_intake_is_closed_when_the_provider_has_no_image_support():
    client, store, runtime, admin, operator, viewer = _client(
        capabilities=ModelCapabilities(image=False)
    )
    image = base64.b64encode(b"fake-jpeg-bytes").decode()

    response = client.post(
        "/api/v1/intake/photo",
        json={"sender_id": CUSTOMER_PHONE, "image_base64": image},
        headers=_headers(operator),
    )

    assert response.status_code == 503
    assert store.orders == []


def test_malformed_or_oversized_media_is_rejected_before_any_turn():
    client, store, runtime, admin, operator, viewer = _client()

    assert (
        client.post(
            "/api/v1/intake/photo",
            json={"sender_id": CUSTOMER_PHONE, "image_base64": "not base64!!"},
            headers=_headers(operator),
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/intake/voice",
            json={
                "caller": CUSTOMER_PHONE,
                "audio_base64": base64.b64encode(b"x").decode(),
                "mime_type": "application/pdf",
            },
            headers=_headers(operator),
        ).status_code
        == 422
    )
    huge = base64.b64encode(b"x" * (MAX_MEDIA_BYTES + 1)).decode()
    assert (
        client.post(
            "/api/v1/intake/photo",
            json={"sender_id": CUSTOMER_PHONE, "image_base64": huge},
            headers=_headers(operator),
        ).status_code
        == 413
    )
    assert runtime.turns == []


def _seed_orders(store):
    specs = [
        ("ord_a", CUSTOMER_PHONE, "ChemFab Industries", OrderStatus.APPROVED,
         "2026-11-20T10:00:00+00:00"),
        ("ord_b", "+919812345002", "Other Corp", OrderStatus.PENDING_REVIEW,
         "2026-11-21T10:00:00+00:00"),
        ("ord_c", CUSTOMER_PHONE, "ChemFab Industries", OrderStatus.DISPATCHED,
         "2026-11-22T10:00:00+00:00"),
    ]
    for order_id, phone, customer, status, created_at in specs:
        asyncio.run(
            store.create_order(
                Order(
                    order_id=order_id,
                    phone=phone,
                    customer=customer,
                    status=status,
                    created_at=created_at,
                    items=[OrderItem(product="sulfuric acid", quantity=1, unit="kg")],
                    escalation_reasons=["unknown_customer"]
                    if status is OrderStatus.PENDING_REVIEW
                    else [],
                )
            )
        )


def _ids(response):
    return [order["order_id"] for order in response.json()]


def test_order_list_is_readable_by_all_roles_and_sorted_newest_first():
    client, store, runtime, admin, operator, viewer = _client()
    _seed_orders(store)

    assert client.get("/api/v1/orders").status_code == 401
    response = client.get("/api/v1/orders", headers=_headers(viewer))

    assert response.status_code == 200
    assert _ids(response) == ["ord_c", "ord_b", "ord_a"]


def test_order_list_filters_by_status_customer_phone_and_date():
    client, store, runtime, admin, operator, viewer = _client()
    _seed_orders(store)
    headers = _headers(operator)

    pending = client.get("/api/v1/orders?status=pending_review", headers=headers)
    assert _ids(pending) == ["ord_b"]
    assert _ids(
        client.get(
            f"/api/v1/orders?phone={quote(CUSTOMER_PHONE, safe='')}", headers=headers
        )
    ) == ["ord_c", "ord_a"]
    assert _ids(client.get("/api/v1/orders?customer=chemfab", headers=headers)) == [
        "ord_c", "ord_a"
    ]
    assert _ids(client.get("/api/v1/orders?date=2026-11-21", headers=headers)) == [
        "ord_b"
    ]
    assert _ids(
        client.get(
            f"/api/v1/orders?status=dispatched&phone={quote(CUSTOMER_PHONE, safe='')}",
            headers=headers,
        )
    ) == ["ord_c"]


def test_invalid_order_filters_are_client_errors():
    client, store, runtime, admin, operator, viewer = _client()
    _seed_orders(store)

    assert (
        client.get(
            "/api/v1/orders?status=nonsense", headers=_headers(operator)
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/v1/orders?date=not-a-date", headers=_headers(operator)
        ).status_code
        == 422
    )


def test_order_detail_carries_the_event_timeline_and_voucher_metadata():
    client, store, runtime, admin, operator, viewer = _client()
    _seed_orders(store)
    asyncio.run(
        store.append_order_event(
            OrderEvent(
                order_id="ord_a",
                event_type="voucher_ready",
                payload={"voucher_id": "voucher_ord_a"},
                created_at="2026-11-20T11:00:00+00:00",
            )
        )
    )
    asyncio.run(
        store.append_order_event(
            OrderEvent(
                order_id="ord_a",
                event_type="order_created",
                payload={"phone": CUSTOMER_PHONE},
                created_at="2026-11-20T10:00:00+00:00",
            )
        )
    )
    order = asyncio.run(store.get_order("ord_a"))
    order.voucher_id = "voucher_ord_a"
    asyncio.run(store.update_order(order))

    assert client.get("/api/v1/orders/ord_a").status_code == 401
    response = client.get("/api/v1/orders/ord_a", headers=_headers(viewer))

    assert response.status_code == 200
    body = response.json()
    assert body["order_id"] == "ord_a"
    assert body["items"][0]["product"] == "sulfuric acid"
    assert body["voucher"] == {
        "prepared": True,
        "voucher_id": "voucher_ord_a",
    }
    assert [event["event_type"] for event in body["events"]] == [
        "order_created",
        "voucher_ready",
    ]
    assert body["events"][0]["payload"]["phone"] == CUSTOMER_PHONE


def test_order_detail_for_an_unknown_order_is_404():
    client, store, runtime, admin, operator, viewer = _client()

    assert (
        client.get("/api/v1/orders/ord_missing", headers=_headers(operator)).status_code
        == 404
    )


def test_escalated_intake_returns_the_escalation_reasons():
    client, store, runtime, admin, operator, viewer = _client(confidence=0.1)

    response = client.post(
        "/api/v1/intake",
        json={"sender_id": CUSTOMER_PHONE, "message": "unsure about this order"},
        headers=_headers(operator),
    )

    assert response.status_code == 200
    decision = response.json()["decision"]
    assert decision["approved"] is False
    assert decision["status"] == "pending_review"
    assert "low_confidence" in decision["escalation_reasons"]


def test_openapi_serves_the_versioned_intake_and_orders_contract():
    client, store, runtime, admin, operator, viewer = _client()

    response = client.get("/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/api/v1/intake" in paths
    assert "/api/v1/intake/voice" in paths
    assert "/api/v1/intake/photo" in paths
    assert "/api/v1/orders" in paths
    assert "/api/v1/orders/{order_id}" in paths
    assert client.get("/docs").status_code == 200


def test_intake_rejects_an_invalid_sender_number():
    client, store, runtime, admin, operator, viewer = _client()

    assert (
        client.post(
            "/api/v1/intake",
            json={"sender_id": "not-a-phone", "message": "an order"},
            headers=_headers(operator),
        ).status_code
        == 422
    )


@pytest.mark.parametrize("role", list(Role))
def test_every_read_role_can_inspect_orders(role):
    client, store, runtime, admin, operator, viewer = _client()
    _seed_orders(store)
    payload = {"name": role.value, "role": role.value}
    if role is Role.APPROVER:
        payload["phone"] = asyncio.run(store.get_approvers())[0].phone
    issued = client.post(
        "/api/v1/keys",
        json=payload,
        headers=_headers(admin),
    )
    assert issued.status_code == 201
    headers = _headers(issued.json()["token"])

    assert client.get("/api/v1/orders", headers=headers).status_code == 200
    assert client.get("/api/v1/orders/ord_a", headers=headers).status_code == 200
