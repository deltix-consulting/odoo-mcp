"""Tests for the odoo_help meta-tool.

These verify the capability overview response shape, and — importantly —
that calling ``odoo_help`` never triggers authentication against Odoo.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from odoo_mcp.audit import AuditLog
from odoo_mcp.client import OdooClient
from odoo_mcp.config import AppConfig, Defaults, InstanceConfig
from odoo_mcp.credentials import Credentials
from odoo_mcp.dispatcher import (
    _HELP_TOOLS_TERSE,
    Dispatcher,
    InstanceRuntime,
    OdooMcpApp,
    hidden_tool_names,
)
from odoo_mcp.security.limits import RateLimiter
from odoo_mcp.security.prod_guard import ProdGuard
from odoo_mcp.tools import build_tools


def _instance_config(name: str = "prod", production: bool = True) -> InstanceConfig:
    return InstanceConfig(
        name=name,
        url="https://example.odoo.com",
        database="db",
        credentials_env_prefix="ODOO_MCP_PROD",
        production=production,
        timeout_seconds=30,
        max_records_default=50,
        max_records_hard_cap=500,
        rate_limit_per_minute=60,
        allow_self_signed=False,
        allowed_models=frozenset({"res.partner", "crm.lead"}),
    )


def _build_app(tmp_path: Path) -> OdooMcpApp:
    inst_cfg = _instance_config()
    creds = Credentials(instance_name=inst_cfg.name, username="u", _api_key="k" * 10)
    client = OdooClient(inst_cfg, credentials=creds)
    app_cfg = AppConfig(
        path=tmp_path / "config.toml",
        defaults=Defaults(),
        instances={inst_cfg.name: inst_cfg},
        audit_log_path=tmp_path / "audit.jsonl",
    )
    audit = AuditLog(app_cfg.audit_log_path)
    return OdooMcpApp(
        config=app_cfg,
        audit=audit,
        prod_guard=ProdGuard(),
        rate_limiter=RateLimiter(),
        instances={inst_cfg.name: InstanceRuntime(config=inst_cfg, client=client)},
    )


def _call(
    dispatcher: Dispatcher, name: str, args: dict[str, object] | None = None
) -> dict[str, object]:
    contents = asyncio.run(dispatcher.call(name, args or {}))
    assert len(contents) == 1
    payload: dict[str, object] = json.loads(contents[0].text)
    return payload


def test_help_is_registered_as_first_tool() -> None:
    tools = build_tools()
    assert tools[0].name == "odoo_help"
    assert "Never contacts Odoo" in (tools[0].description or "")


def test_help_returns_expected_structure(tmp_path: Path) -> None:
    """Verbose mode preserves the v0.10.x cookbook shape."""
    app = _build_app(tmp_path)
    dispatcher = Dispatcher(app)

    payload = _call(dispatcher, "odoo_help", {"verbose": True})

    assert payload["ok"] is True
    assert "version" in payload
    assert isinstance(payload["summary"], str) and payload["summary"]
    assert isinstance(payload["common_patterns"], list) and payload["common_patterns"]
    assert isinstance(payload["gotchas"], list) and payload["gotchas"]
    assert isinstance(payload["instances"], list) and payload["instances"]
    # Each common pattern carries at least a goal + use.
    for pattern in payload["common_patterns"]:
        assert isinstance(pattern, dict)
        assert "goal" in pattern
        assert "use" in pattern
    # Each instance carries the metadata shape we promise.
    inst = payload["instances"][0]
    assert isinstance(inst, dict)
    for key in ("name", "url", "database", "production", "writes_unlocked", "allowed_models"):
        assert key in inst


def test_help_default_is_terse(tmp_path: Path) -> None:
    """Default mode drops common_patterns/gotchas in favour of a tools list."""
    app = _build_app(tmp_path)
    dispatcher = Dispatcher(app)

    payload = _call(dispatcher, "odoo_help")

    assert payload["ok"] is True
    assert "tools" in payload
    assert "common_patterns" not in payload
    assert "gotchas" not in payload


def test_help_does_not_authenticate(tmp_path: Path) -> None:
    """Must never call ensure_authenticated — _uid stays None."""
    app = _build_app(tmp_path)
    dispatcher = Dispatcher(app)
    client = app.instances["prod"].client

    assert client._uid is None
    _call(dispatcher, "odoo_help")
    # The help call neither authenticated nor reached out over the network.
    assert client._uid is None


def test_help_audit_uses_help_op(tmp_path: Path) -> None:
    """`odoo_help` must record op='help' in the audit log, not 'fields_get'."""
    app = _build_app(tmp_path)
    dispatcher = Dispatcher(app)

    _call(dispatcher, "odoo_help")

    raw = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    # Last line is the help call's audit entry (the first is the open marker).
    last = json.loads(raw[-1])
    assert last["tool"] == "odoo_help"
    assert last["op"] == "help"


def test_list_instances_audit_uses_list_instances_op(tmp_path: Path) -> None:
    """`odoo_list_instances` must record op='list_instances'."""
    app = _build_app(tmp_path)
    dispatcher = Dispatcher(app)

    _call(dispatcher, "odoo_list_instances")

    raw = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    last = json.loads(raw[-1])
    assert last["tool"] == "odoo_list_instances"
    assert last["op"] == "list_instances"


def test_help_and_list_instances_are_read_ops() -> None:
    """The new ops must be classified as read ops, not write ops."""
    from odoo_mcp.security.allowlist import Operation, is_read, is_write

    assert is_read(Operation.HELP)
    assert is_read(Operation.LIST_INSTANCES)
    assert not is_write(Operation.HELP)
    assert not is_write(Operation.LIST_INSTANCES)


def _help_tool_names(dispatcher: Dispatcher) -> list[str]:
    payload = _call(dispatcher, "odoo_help")
    tools = payload["tools"]
    assert isinstance(tools, list)
    return [t["name"] for t in tools]


def test_help_catalogue_matches_the_served_tool_list() -> None:
    """``odoo_help`` must name every tool ``build_tools`` serves, in that order.

    ``_HELP_TOOLS_TERSE`` is a hand-maintained literal that was correct in
    v0.11.0 and then stopped being extended: v0.27.0 served 18 tools and the
    catalogue named 14, silently omitting ``odoo_send_message``,
    ``odoo_log_note``, ``odoo_run_document_action`` and
    ``odoo_create_attachment``. Pin the parity so adding a tool without a
    catalogue entry fails here instead of in a customer's session.
    """
    assert [t["name"] for t in _HELP_TOOLS_TERSE] == [t.name for t in build_tools()]


def test_help_catalogue_entries_are_one_liners() -> None:
    """Every catalogue entry carries a non-empty purpose (terse mode's whole job)."""
    for entry in _HELP_TOOLS_TERSE:
        assert set(entry) == {"name", "purpose"}
        assert entry["purpose"].strip()


def test_help_omits_a_tool_disabled_by_env(tmp_path: Path, monkeypatch: Any) -> None:
    """A tool hidden from tools/list must not be advertised by odoo_help either.

    The dispatcher refuses ``ODOO_MCP_DISABLE_TOOLS`` names per call, and
    ``build_server`` drops them from the advertisement. The help catalogue is
    the third renderer of the same state and has to agree.
    """
    dispatcher = Dispatcher(_build_app(tmp_path))

    monkeypatch.delenv("ODOO_MCP_DISABLE_TOOLS", raising=False)
    assert "odoo_write" in _help_tool_names(dispatcher)

    monkeypatch.setenv("ODOO_MCP_DISABLE_TOOLS", "odoo_write, odoo_archive_or_delete")
    names = _help_tool_names(dispatcher)
    assert "odoo_write" not in names
    assert "odoo_archive_or_delete" not in names
    # Everything else survives the filter.
    assert "odoo_search_read" in names


def test_help_gates_send_message_on_the_double_opt_in(tmp_path: Path, monkeypatch: Any) -> None:
    """``odoo_send_message`` is advertised only when both opt-ins are satisfied."""
    monkeypatch.delenv("ODOO_MCP_DISABLE_TOOLS", raising=False)

    # Neither gate: hidden.
    monkeypatch.delenv("ODOO_MCP_ENABLE_EXTERNAL_COMMS", raising=False)
    closed = _build_app(tmp_path)
    assert "odoo_send_message" not in _help_tool_names(Dispatcher(closed))

    # Env var only, instance flag still false: still hidden.
    monkeypatch.setenv("ODOO_MCP_ENABLE_EXTERNAL_COMMS", "1")
    assert "odoo_send_message" not in _help_tool_names(Dispatcher(closed))

    # Both gates: advertised.
    opened = _build_app(tmp_path)
    for name, rt in opened.instances.items():
        opened.instances[name] = InstanceRuntime(
            config=replace(rt.config, external_comms_enabled=True),
            client=rt.client,
        )
    assert "odoo_send_message" in _help_tool_names(Dispatcher(opened))


def test_help_catalogue_filter_agrees_with_build_server(tmp_path: Path, monkeypatch: Any) -> None:
    """The advertisement and the catalogue are filtered by the same helper."""
    monkeypatch.setenv("ODOO_MCP_DISABLE_TOOLS", "odoo_read_group")
    monkeypatch.delenv("ODOO_MCP_ENABLE_EXTERNAL_COMMS", raising=False)
    app = _build_app(tmp_path)

    hidden = hidden_tool_names(app)
    advertised = [t.name for t in build_tools() if t.name not in hidden]

    assert _help_tool_names(Dispatcher(app)) == advertised
    assert "odoo_read_group" not in advertised
    assert "odoo_send_message" not in advertised
