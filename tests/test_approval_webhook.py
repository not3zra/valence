"""End-to-end WhatsApp approval flow (issue #7).

An escalated order (unverified number) sends an approval-requested notification
to every allowlisted approver over WhatsApp and records an Order Event. The
approver's confirm/reject reply goes to the same agent, which invokes the
approve tool; the order transitions through the core and the approver gets a
brief confirmation. A non-allowlisted number with nothing pending is ignored.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from google.adk.sessions import InMemorySessionService

from src.agent import build_agent
from src.orders import OrderStatus
from src.store import InMemoryOrderStore
from src.twilio import build_twilio_signature
from src.web import create_app
from src.whatsapp import MockWhatsAppSender

from .fakes import ApprovingToolCallingLlm, ConfirmingToolCallingLlm

WEBHOOK_URL = "/api/whatsapp/webhook"
AUTH_TOKEN = "test-auth-token"
APPROVER_PHONE = "+919845000001"  # a_nikhil


def _sign(form: dict[str, str]) -> str:
    return build_twilio_signature(
        f"http://testserver{WEBHOOK_URL}", form, AUTH_TOKEN
    )


def _escalating_form() -> dict[str, str]:
    # An unverified number escalates the order (unknown_customer), which is
    # what triggers the approval-requested notification to approvers.
    return {
        "From": "whatsapp:+919999999999",
        "Body": "500 drums of sulfuric acid please",
        "NumMedia": "0",
    }


def _reply_form(phone: str, body: str) -> dict[str, str]:
    return {
        "From": f"whatsapp:{phone}",
        "Body": body,
        "NumMedia": "0",
    }


def _escalate(sender) -> InMemoryOrderStore:
    store = InMemoryOrderStore()
    app = create_app(
        agent=build_agent(
            model=ConfirmingToolCallingLlm(), store=store, whatsapp_sender=sender
        ),
        session_service=InMemorySessionService(),
        whatsapp_sender=sender,
        twilio_auth_token=AUTH_TOKEN,
    )
    form = _escalating_form()
    client = TestClient(app)
    response = client.post(
        WEBHOOK_URL, data=form, headers={"X-Twilio-Signature": _sign(form)}
    )
    assert response.status_code == 200
    return store


def _decide(store, sender, phone, body, approved) -> None:
    app = create_app(
        agent=build_agent(
            model=ApprovingToolCallingLlm(approved=approved),
            store=store,
            whatsapp_sender=sender,
        ),
        session_service=InMemorySessionService(),
        whatsapp_sender=sender,
        twilio_auth_token=AUTH_TOKEN,
    )
    form = _reply_form(phone, body)
    response = TestClient(app).post(
        WEBHOOK_URL, data=form, headers={"X-Twilio-Signature": _sign(form)}
    )
    assert response.status_code == 200


def _order(store) -> object:
    return store.orders[-1]


def test_escalation_notifies_approvers_then_approver_confirm_approves():
    sender = MockWhatsAppSender()
    store = _escalate(sender)
    escalated = _order(store)
    assert escalated.status is OrderStatus.PENDING_REVIEW

    # The escalation notifies every allowlisted approver over WhatsApp and
    # records the request as an Order Event (not a clarify turn).
    approver_sends = [s for s in sender.sent if s[0] == APPROVER_PHONE]
    assert len(approver_sends) == 1
    assert escalated.order_id in approver_sends[0][1]
    assert any(
        e.event_type == "order_approval_requested"
        and e.order_id == escalated.order_id
        for e in store.events
    )
    assert store.pending_approvals[APPROVER_PHONE] == escalated.order_id

    # The approver replies "ok" -> the agent parses a confirm verb and the
    # approve tool moves the order through the core.
    _decide(store, sender, APPROVER_PHONE, "ok", approved=True)

    assert _order(store).status is OrderStatus.APPROVED
    assert any(
        e.event_type == "order_approved" and e.order_id == escalated.order_id
        for e in store.events
    )
    assert APPROVER_PHONE not in store.pending_approvals
    assert "approved" in sender.sent[-1][1]


def test_escalation_then_approver_reject_marks_order_rejected():
    sender = MockWhatsAppSender()
    store = _escalate(sender)
    escalated = _order(store)

    _decide(store, sender, APPROVER_PHONE, "reject", approved=False)

    assert _order(store).status is OrderStatus.REJECTED
    assert any(
        e.event_type == "order_rejected" and e.order_id == escalated.order_id
        for e in store.events
    )
    # A rejected order stays visible for correction (still queryable).
    assert _order(store) is not None


def test_non_allowlisted_number_reply_is_ignored():
    sender = MockWhatsAppSender()
    store = _escalate(sender)

    # Only allowlisted approvers got a pending entry.
    assert "+919812345001" not in store.pending_approvals

    # A non-approver replying "confirm" must not move the order.
    _decide(store, sender, "+919812345001", "confirm", approved=True)

    assert _order(store).status is OrderStatus.PENDING_REVIEW


def test_one_approvers_decision_clears_pending_for_every_other_approver():
    sender = MockWhatsAppSender()
    store = _escalate(sender)
    escalated = _order(store)
    second_approver = "+919845000002"  # another allowlisted approver
    assert store.pending_approvals[second_approver] == escalated.order_id

    # The first approver decides; the second's stale pending entry is cleared
    # too, so a later reply from them can never act on the decided order.
    _decide(store, sender, APPROVER_PHONE, "ok", approved=True)

    assert second_approver not in store.pending_approvals
    assert _order(store).status is OrderStatus.APPROVED
