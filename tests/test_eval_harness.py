"""Tests for the agent eval harness (issue #36).

The harness runs the real agent against a real Gemini model, so most of its
behavior is exercised through fake models exactly like the rest of the suite
fakes the model boundary. These tests pin the harness machinery itself: the
tool trace recorder, the pinned clock, hard vs soft failure classification, the
case registry, and — critically — the issue #34 accumulation regression gate
driven end-to-end against a scripted multi-turn fake model.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import AsyncGenerator
from unittest import mock

from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions import InMemorySessionService
from google.genai import types

from scripts.eval_agent import (
    CASES,
    CHEMFAB,
    EvalCase,
    EvalHarness,
    PinnedClock,
    ToolTrace,
    Turn,
    _has_key,
    _no_privileged_tools,
    _select_cases,
    _summarize,
    main,
)
from src.agent import build_agent, build_runner, run_turn
from src.orders import OrderStatus
from src.store import InMemoryOrderStore


class MergingToolCallingLlm(BaseLlm):
    """Scripts the #34 accumulation: three inbound messages, one merged order.

    Turn 1 submits a partial line (sulfuric, no location); the tool replies
    clarify. Turn 2 adds a second line (caustic); the tool merges it into the
    held partial and still clarifies. Turn 3 supplies the missing location; the
    merged order is re-run through the core and approved. Reading the real tool
    result from the second ADK turn keeps the fake honest about what the tool
    actually decided.
    """

    model: str = "fake-merging"

    _inbound = 0

    def supported_models(self) -> list[str]:
        return ["fake-merging.*"]

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        contents = llm_request.contents
        last_inbound = -1
        last_result = -1
        for i, content in enumerate(contents):
            if content.role == "user" and not any(
                part.function_response for part in content.parts
            ):
                last_inbound = i
            elif content.role == "user":
                last_result = i

        if last_result > last_inbound:
            result = contents[last_result].parts[0].function_response.response
            if result.get("clarify"):
                text = "Anything else? And where should we deliver?"
            else:
                total = float(result["draft_value_inr"])
                text = f"Order confirmed. Estimated total: {total:,.0f} INR."
            yield LlmResponse(
                content=types.Content(
                    role="model", parts=[types.Part(text=text)]
                )
            )
            return

        turn = self._inbound
        self._inbound = turn + 1
        if turn == 0:
            args = {
                "items": [
                    {"product": "sulfuric acid", "quantity": 2000, "unit": "kg"}
                ],
                "confidence": 0.9,
                "source_channel": "whatsapp",
            }
        elif turn == 1:
            args = {
                "items": [
                    {"product": "caustic soda", "quantity": 500, "unit": "kg"}
                ],
                "confidence": 0.9,
                "source_channel": "whatsapp",
            }
        else:
            args = {
                "items": [
                    {"product": "caustic soda", "quantity": 500, "unit": "kg"}
                ],
                "delivery_location": "Peenya Industrial Area",
                "confidence": 0.9,
                "source_channel": "whatsapp",
            }
        call = types.FunctionCall(name="process_order", args=args)
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part(function_call=call)])
        )


class DoneToolCallingLlm(BaseLlm):
    """Calls ``process_order`` once with a clean approved order, then confirms.

    The single-turn happy-path stand-in: mirrors ``ToolCallingLlm`` but reads
    the real tool result so the reply reflects the committed order.
    """

    model: str = "fake-done"

    def supported_models(self) -> list[str]:
        return ["fake-done.*"]

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        contents = llm_request.contents
        last_inbound = -1
        last_result = -1
        for i, content in enumerate(contents):
            if content.role == "user" and not any(
                part.function_response for part in content.parts
            ):
                last_inbound = i
            elif content.role == "user":
                last_result = i

        if last_result > last_inbound:
            result = contents[last_result].parts[0].function_response.response
            total = float(result["draft_value_inr"])
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(
                            text=f"Order confirmed. Estimated total: {total:,.0f} INR."
                        )
                    ],
                )
            )
            return

        call = types.FunctionCall(
            name="process_order",
            args={
                "items": [
                    {"product": "sulfuric acid", "quantity": 2000, "unit": "kg"}
                ],
                "delivery_location": "Peenya Industrial Area",
                "confidence": 0.9,
                "source_channel": "whatsapp",
            },
        )
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part(function_call=call)])
        )


def _fresh_agent(store: InMemoryOrderStore, model=DoneToolCallingLlm()):
    return build_agent(model=model, store=store)


def _run_turn_into_trace(llm, message: str = "order") -> tuple[ToolTrace, list[str]]:
    store = InMemoryOrderStore()
    runner = build_runner(build_agent(model=llm, store=store), InMemorySessionService())
    trace = ToolTrace()
    replies = [
        run_turn(runner, sender_id=CHEMFAB, message=message, on_event=trace.on_event)
    ]
    return trace, replies


def test_trace_records_tool_calls_and_results() -> None:
    trace, _ = _run_turn_into_trace(DoneToolCallingLlm())
    assert trace.tool_names == {"process_order"}
    result = trace.last_result("process_order")
    assert result is not None
    assert result["approved"] is True
    assert result["delivery_location_id"] == "dl_peenya"


def test_pinned_clock_freezes_now_across_core_orders_loading() -> None:
    import datetime as dt

    import src.core
    import src.loading
    import src.orders

    before = dt.datetime.now()
    with PinnedClock() as clock:
        pinned = clock.value
        assert src.core.datetime.now() == pinned
        assert src.orders.datetime.now() == pinned
        assert src.loading.datetime.now() == pinned
        clock.advance(minutes=90)
        assert src.core.datetime.now() == pinned + dt.timedelta(minutes=90)
    # The real wall clock is restored after the context exits.
    assert abs((dt.datetime.now() - before).total_seconds()) < 30


def test_harness_runs_a_clean_case_and_passes() -> None:
    case = EvalCase(
        name="unit-clean",
        category="unit",
        steps=[Turn(CHEMFAB, "2 drums sulfuric acid, Peenya Industrial Area")],
        check=lambda o: (
            ["expected exactly one order, got none"]
            if len(o.orders_for(CHEMFAB)) != 1
            else []
        ),
    )
    result = EvalHarness(_fresh_agent).run_case(case)
    assert result.passed
    assert result.hard_failures == []
    assert result.error is None
    assert result.outcome.store.orders[0].status is OrderStatus.APPROVED


def test_hard_failure_fails_case_and_reports_expected_vs_produced() -> None:
    case = EvalCase(
        name="unit-hard-fail",
        category="unit",
        steps=[Turn(CHEMFAB, "2 drums sulfuric acid, Peenya Industrial Area")],
        check=lambda o: [
            "expected the order escalated, got APPROVED"
            if o.store.orders
            else "expected a committed order, got none"
        ],
    )
    result = EvalHarness(_fresh_agent).run_case(case)
    assert not result.passed
    assert result.hard_failures
    assert "APPROVED" in result.hard_failures[0]


def test_soft_failure_is_lenient() -> None:
    case = EvalCase(
        name="unit-soft",
        category="unit",
        steps=[Turn(CHEMFAB, "2 drums sulfuric acid, Peenya Industrial Area")],
        check=lambda o: [],
        soft_check=lambda o: ["reply text off-brand"],
    )
    result = EvalHarness(_fresh_agent).run_case(case)
    assert result.passed
    assert result.soft_failures == ["reply text off-brand"]


def test_case_error_is_reported_not_raised() -> None:
    case = EvalCase(
        name="unit-missing-media",
        category="unit",
        steps=[Turn(CHEMFAB, "read this", media="does-not-exist.png")],
        check=lambda o: [],
    )
    result = EvalHarness(_fresh_agent).run_case(case)
    assert not result.passed
    assert result.error is not None
    assert "FileNotFoundError" in result.error
    assert result.hard_failures == []


def test_accumulation_case_gate_regression_issue_34() -> None:
    """The #34 gate: a partial order accumulates across clarifying turns."""
    case = next(c for c in CASES if c.name == "accumulate-lines-across-clarify")

    def build(store: InMemoryOrderStore):
        return build_agent(model=MergingToolCallingLlm(), store=store)

    result = EvalHarness(build).run_case(case)
    assert result.passed, result.hard_failures
    assert result.error is None

    order = result.outcome.store.orders[0]
    assert order.status is OrderStatus.APPROVED
    by_product = {item.product.lower(): item for item in order.items}
    assert "sulfuric acid" in by_product
    assert "caustic soda" in by_product
    assert by_product["caustic soda"].quantity == 500
    assert order.delivery_location_id == "dl_peenya"


def test_resend_after_dedup_window_is_not_a_duplicate() -> None:
    """``advance_minutes`` steps the pinned clock across the dedup window."""
    case = next(c for c in CASES if c.name == "robustness-resend-after-window")

    def build(store: InMemoryOrderStore):
        return build_agent(model=DoneToolCallingLlm(), store=store)

    result = EvalHarness(build).run_case(case)
    assert result.passed, result.hard_failures
    assert len(result.outcome.store.orders) == 2
    assert not result.outcome.trace.last_result("process_order")["duplicate"]


def test_late_order_case_flags_the_after_cutoff_order() -> None:
    """The pinned per-case clock makes the late-order case deterministic."""
    case = next(c for c in CASES if c.name == "dispatch-late-order")

    def build(store: InMemoryOrderStore):
        return build_agent(model=DoneToolCallingLlm(), store=store)

    result = EvalHarness(build).run_case(case)
    assert result.passed, result.hard_failures
    decision = result.outcome.trace.last_result("process_order")
    assert decision["late"] is True
    assert decision["approved"] is True


def test_no_privileged_tools_fails_when_a_safety_tool_is_called() -> None:
    trace = ToolTrace()
    trace.calls.append({"name": "render_loading_list", "args": {}})
    outcome = type(
        "Outcome", (), {"trace": trace, "store": InMemoryOrderStore(), "replies": []}
    )()
    failures = _no_privileged_tools(outcome)
    assert failures
    assert "render_loading_list" in failures[0]


def test_no_privileged_tools_passes_for_a_clean_run() -> None:
    trace, _ = _run_turn_into_trace(DoneToolCallingLlm())
    outcome = type(
        "Outcome", (), {"trace": trace, "store": InMemoryOrderStore(), "replies": []}
    )()
    assert _no_privileged_tools(outcome) == []


def test_registry_has_expected_case_set() -> None:
    assert len(CASES) >= 20
    names = [case.name for case in CASES]
    assert len(names) == len(set(names))
    assert all(case.steps for case in CASES)
    assert all(case.check is not None for case in CASES)
    assert all(case.category for case in CASES)
    categories = {case.category for case in CASES}
    assert {"safety", "approver", "accumulation", "audio"} <= categories


def test_select_cases_filters_by_category_and_name() -> None:
    args = argparse.Namespace(category="safety", cases="")
    safety = _select_cases(args)
    assert safety and all(case.category == "safety" for case in safety)

    args = argparse.Namespace(category="", cases="happy-hindi-text")
    picked = _select_cases(args)
    assert [case.name for case in picked] == ["happy-hindi-text"]


def test_main_gates_real_runs_on_an_api_key() -> None:
    with mock.patch("scripts.eval_agent._has_key", return_value=False):
        assert main([]) == 2
    # --list needs no key.
    assert main(["--list"]) == 0


def test_has_key_accepts_a_complete_vertex_config() -> None:
    base = {
        "GOOGLE_API_KEY": None,
        "GEMINI_API_KEY": None,
        "GOOGLE_GENAI_USE_VERTEXAI": "true",
        "GOOGLE_CLOUD_PROJECT": "valence-505412",
        "GOOGLE_CLOUD_LOCATION": "us-central1",
    }
    for cased in ("true", "1", "True"):
        with mock.patch.dict(
            os.environ, {k: v for k, v in base.items() if v}, clear=True
        ) as env:
            env["GOOGLE_GENAI_USE_VERTEXAI"] = cased
            assert _has_key()
    for drop in (
        "GOOGLE_CLOUD_LOCATION",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_GENAI_USE_VERTEXAI",
    ):
        with mock.patch.dict(
            os.environ, {k: v for k, v in base.items() if v}, clear=True
        ) as env:
            env.pop(drop, None)
            assert not _has_key()


def test_summarize_groups_results_by_case_category() -> None:
    """Regression gate: the report groups ``CaseResult``s, not cases."""
    case = EvalCase(
        name="unit-report",
        category="unit",
        steps=[Turn(CHEMFAB, "2 drums sulfuric acid, Peenya Industrial Area")],
        check=lambda o: [],
    )
    result = EvalHarness(_fresh_agent).run_case(case)
    # The report is pure output; 0 means the whole set passed and the
    # grouping did not crash on the CaseResult shape.
    assert _summarize([result]) == 0


def test_harness_delay_paces_turns_between_cases() -> None:
    """A positive ``delay`` sleeps before each turn (free-tier quota pacing)."""
    case = EvalCase(
        name="unit-delay",
        category="unit",
        steps=[Turn(CHEMFAB, "2 drums sulfuric acid, Peenya Industrial Area")],
        check=lambda o: [],
    )
    with mock.patch("scripts.eval_agent.time.sleep") as sleep:
        result = EvalHarness(_fresh_agent, delay=2.0).run_case(case)
        assert result.passed
        assert sleep.call_count == len(case.steps)
    with mock.patch("scripts.eval_agent.time.sleep") as sleep:
        EvalHarness(_fresh_agent, delay=0.0).run_case(case)
        sleep.assert_not_called()
