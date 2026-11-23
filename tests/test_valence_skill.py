"""Valence Skill contract: documentation, install snippets and runnable examples (#64).

The Skill is the instruction set an external agent loads before it touches an
Instance. These tests keep that instruction set honest: the documented tool and
safety contract must match the MCP server, the install snippets must produce a
command the real CLI accepts, and every worked example must actually run over the
MCP protocol.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest

from src.auth import Principal, Role
from src.cli import _parser
from src.intake import IntakeResult
from src.mcp_auth import McpToolGateway
from src.mcp_server import MCP_TOOL_NAMES, build_mcp_operations, build_mcp_server
from tests.test_mcp_server import _auth, _protocol_call

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "skills" / "valence"
PHONE = "+919812345001"
REASONS = ["missing_field: delivery_location"]


def _read(name: str) -> str:
    return (SKILL_DIR / name).read_text(encoding="utf-8")


def _frontmatter(text: str) -> dict[str, str]:
    assert text.startswith("---\n")
    block = text.split("---", 2)[1]
    return dict(
        line.split(": ", 1) for line in block.strip().splitlines() if ": " in line
    )


def _section(text: str, heading: str) -> str:
    match = re.search(
        rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S
    )
    assert match, f"missing '## {heading}' section"
    return match.group(1)


def _blocks(section: str, language: str) -> list[str]:
    return re.findall(rf"```{language}\n(.*?)```", section, re.S)


def _examples() -> dict[str, Any]:
    return json.loads(_read("examples.json"))


def test_skill_frontmatter_names_and_triggers_the_skill():
    meta = _frontmatter(_read("SKILL.md"))

    assert meta["name"] == "valence"
    assert "Valence" in meta["description"]
    assert "Use when" in meta["description"]
    assert re.search(r"order|Agent Interface|Instance", meta["description"], re.I)


def test_skill_documents_when_connection_tools_and_workflows():
    skill = _read("SKILL.md")

    for heading in ("When to use", "Connection", "Tools", "Workflows", "Safety rules"):
        _section(skill, heading)
    for name in ("VALENCE_URL", "VALENCE_API_KEY", "/mcp", "examples.json"):
        assert name in skill
    for tool in MCP_TOOL_NAMES:
        assert f"`{tool}`" in skill


def test_skill_states_the_four_safety_rules():
    skill = _read("SKILL.md").lower()

    assert "explicit human confirmation" in skill
    assert re.search(r"approv\w+.*bill\w+|bill\w+.*approv\w+", skill)
    assert "untrusted" in skill and "intake" in skill
    assert "fabricat" in skill
    assert "escalation reasons" in skill and "surfac" in skill


def test_install_snippets_are_valid_for_claude_code_codex_and_opencode():
    install = _read("INSTALL.md")

    claude = _section(install, "Claude Code")
    claude_cfg = json.loads(_blocks(claude, "json")[0])
    claude_entry = claude_cfg["mcpServers"]["valence"]
    assert claude_entry["command"] == "valence"
    assert claude_entry["args"] == ["mcp", "--transport", "stdio"]
    assert "VALENCE_API_KEY" in claude_entry["env"]
    assert ".claude/skills/valence" in claude
    assert "claude mcp add" in claude
    assert "--transport stdio" in claude and "--" in claude

    codex = _section(install, "Codex")
    codex_cfg = "\n".join(_blocks(codex, "toml"))
    assert "[mcp_servers.valence]" in codex_cfg
    assert 'command = "valence"' in codex_cfg
    args = ast.literal_eval(
        re.search(r"args = (\[.*?\])", codex_cfg, re.S).group(1)
    )
    assert args == ["mcp", "--transport", "stdio"]
    assert "[mcp_servers.valence.env]" in codex_cfg
    assert "VALENCE_API_KEY" in codex_cfg
    assert "bearer_token_env_var" in codex_cfg
    assert "$HOME/.agents/skills/valence" in codex

    opencode = _section(install, "opencode")
    opencode_cfg = json.loads(_blocks(opencode, "json")[0])
    assert opencode_cfg["$schema"] == "https://opencode.ai/config.json"
    valence = opencode_cfg["mcp"]["servers"]["valence"]
    assert valence["type"] == "local"
    assert valence["command"] == ["valence", "mcp", "--transport", "stdio"]
    assert "VALENCE_API_KEY" in valence["environment"]
    remote = json.loads(_blocks(opencode, "json")[1])["mcp"]["servers"]["valence"]
    assert remote["type"] == "remote"
    assert remote["oauth"] is False
    assert ".opencode/skills/valence" in opencode


def test_install_snippets_use_flags_the_real_cli_accepts():
    install = _read("INSTALL.md")
    claude_cfg = json.loads(_blocks(_section(install, "Claude Code"), "json")[0])
    opencode_cfg = json.loads(_blocks(_section(install, "opencode"), "json")[0])
    codex_cfg = _blocks(_section(install, "Codex"), "toml")[0]
    codex_args = ast.literal_eval(re.search(r"args = (\[.*?\])", codex_cfg).group(1))

    commands = (
        claude_cfg["mcpServers"]["valence"]["args"],
        codex_args,
        opencode_cfg["mcp"]["servers"]["valence"]["command"][1:],
    )
    for argv in commands:
        parsed = _parser().parse_args(argv)
        assert parsed.command == "mcp"
        assert parsed.transport == "stdio"


def test_generic_mcp_client_snippet_uses_both_transports():
    generic = _section(_read("INSTALL.md"), "Generic MCP client")

    assert "valence mcp --transport stdio" in generic
    assert "VALENCE_API_KEY" in generic
    assert "$VALENCE_URL/mcp" in generic
    assert "Authorization: Bearer $VALENCE_API_KEY" in generic


def test_every_documented_example_is_described_in_the_skill():
    skill = _read("SKILL.md")

    names = [example["name"] for example in _examples()["examples"]]
    assert len(names) >= 5
    for name in names:
        assert name in skill


def test_examples_reference_only_registered_tools():
    for example in _examples()["examples"]:
        for step in example["steps"]:
            assert step["tool"] in MCP_TOOL_NAMES


class _Admin:
    async def get_config(self):
        return {"cutoff_time": "16:00"}

    async def list_catalog(self, kind):
        return [{"id": f"{kind}-1", "kind": kind}]


class _Intake:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def intake_text(
        self,
        *,
        sender_id,
        message,
        principal_id,
        idempotency_key=None,
        fingerprint="",
    ):
        self.messages.append(message)
        return IntakeResult(
            reply="ack",
            order_id="ord_1",
            decision={"status": "pending_review", "escalation_reasons": REASONS},
            duplicate=False,
            idempotent=idempotency_key is not None,
        )


class _Orders:
    def __init__(self) -> None:
        self.actions: list[dict[str, Any]] = []

    async def list_orders(self, arguments):
        return [
            {
                "order_id": "ord_1",
                "status": "pending_review",
                "escalation_reasons": REASONS,
            }
        ]

    async def get_order(self, arguments):
        return {
            "order_id": arguments["order_id"],
            "status": "pending_review",
            "escalation_reasons": REASONS,
            "events": [],
        }

    async def act(self, arguments, principal):
        self.actions.append(arguments)
        status = {
            "approve": "approved",
            "prepare_voucher": "approved",
            "bill": "billed",
        }.get(arguments["action"], "done")
        return {
            "order_id": arguments["order_id"],
            "action": arguments["action"],
            "status": status,
            "confirm": arguments.get("confirm", False),
        }

    async def voucher_xml(self, arguments, principal):
        return "<voucher><invoice/></voucher>"


class _Loading:
    async def render_loading_list(self, day):
        return {
            "delivery_day": day.isoformat() if day else "2026-11-23",
            "sections": [],
        }

    async def stats(self):
        return {
            "pending_escalations": 2,
            "approved_today": 1,
            "billed_today": 0,
            "late_orders": 0,
        }


async def _server(principal: str):
    auth, admin, operator, approver, viewer = await _auth()
    keys = {
        "admin": admin,
        "operator": operator,
        "approver": approver,
        "viewer": viewer,
    }
    services = {
        "auth": auth,
        "admin": _Admin(),
        "intake": _Intake(),
        "orders": _Orders(),
        "loading": _Loading(),
    }
    gateway = McpToolGateway(auth, build_mcp_operations(**services))
    return build_mcp_server(gateway, api_key=keys[principal]), services


def _text(result):
    return result.content[0].text


def _matches(actual: Any, expected: Any) -> None:
    if isinstance(expected, dict):
        assert isinstance(actual, dict)
        for key, value in expected.items():
            assert key in actual, f"missing {key}"
            _matches(actual[key], value)
    elif isinstance(expected, list):
        assert isinstance(actual, list)
        assert len(actual) == len(expected)
        for found, wanted in zip(actual, expected, strict=True):
            _matches(found, wanted)
    else:
        assert actual == expected


@pytest.mark.parametrize(
    "example", _examples()["examples"], ids=lambda item: item["name"]
)
def test_documented_example_runs_over_the_mcp_server(example):
    async def run() -> None:
        server, services = await _server(example["principal"])
        for step in example["steps"]:
            result = await _protocol_call(server, step["tool"], step["arguments"])
            if "expect_error" in step:
                assert result.is_error is True
                assert step["expect_error"] in _text(result)
                continue
            assert result.is_error is False, _text(result)
            if step.get("expect_xml", False):
                assert _text(result).lstrip().startswith("<")
            if step.get("expect_no_actions", False):
                assert services["orders"].actions == []
            expect = step.get("expect")
            if expect:
                body = json.loads(_text(result))
                if (
                    isinstance(body, list)
                    and isinstance(expect, dict)
                    and len(body) == 1
                ):
                    body = body[0]
                _matches(body, expect)

    asyncio.run(run())


def test_billing_example_sequence_matches_the_real_service():
    from src.core import OrderProcessingCore
    from src.order_actions import OrderActionService
    from src.seed_data import Approver
    from src.store import InMemoryOrderStore
    from src.voucher import InMemoryVoucherStore
    from tests.fakes import approved_order_id

    billing = next(
        example
        for example in _examples()["examples"]
        if example["name"] == "confirm-billing-with-a-human"
    )
    actions = [
        step["arguments"].get("action")
        for step in billing["steps"]
        if step["tool"] == "order_action" and "expect_error" not in step
    ]
    assert actions == ["prepare_voucher", "bill"]

    async def run() -> None:
        store = InMemoryOrderStore(approvers=[Approver(id="a_test", phone=PHONE)])
        order_id = await approved_order_id(store)
        service = OrderActionService(
            OrderProcessingCore(store), store, InMemoryVoucherStore(), None
        )
        principal = Principal(id="p", name="test", role=Role.APPROVER, phone=PHONE)
        with pytest.raises(ValueError, match="no prepared voucher"):
            await service.act(
                {"order_id": order_id, "action": "bill", "confirm": True}, principal
            )
        await service.act(
            {"order_id": order_id, "action": "prepare_voucher", "confirm": True},
            principal,
        )
        billed = await service.act(
            {"order_id": order_id, "action": "bill", "confirm": True}, principal
        )
        assert billed["status"] == "billed"
        xml = await service.voucher_xml({"order_id": order_id}, principal)
        assert xml.lstrip().startswith("<")

    asyncio.run(run())
