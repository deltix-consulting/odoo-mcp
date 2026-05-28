"""Tests for the odoo_help meta-tool.

These verify the capability overview response shape, and — importantly —
that calling ``odoo_help`` never triggers authentication against Odoo.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from odoo_mcp.audit import AuditLog
from odoo_mcp.client import OdooClient
from odoo_mcp.config import AppConfig, Defaults, InstanceConfig
from odoo_mcp.credentials import Credentials
from odoo_mcp.dispatcher import Dispatcher, InstanceRuntime, OdooMcpApp
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


def test_help_verbose_documents_m2m_command_tuples(tmp_path: Path) -> None:
    """A common_patterns entry must show the [[4, id]] / [[6, 0, [ids]]] shape.

    The #1 stumble we see in agent usage is writing many2many fields as a
    flat id list (e.g. ``tag_ids=[7, 12]``). Odoo silently treats the first
    int as a command, so the write does the wrong thing without raising —
    the kind of footgun the cookbook exists to flag.
    """
    app = _build_app(tmp_path)
    dispatcher = Dispatcher(app)

    payload = _call(dispatcher, "odoo_help", {"verbose": True})
    patterns = payload["common_patterns"]
    assert isinstance(patterns, list)
    m2m_patterns = [p for p in patterns if isinstance(p, dict) and "many2many" in p.get("goal", "")]
    assert len(m2m_patterns) == 1, "expected exactly one m2m write pattern"
    example = m2m_patterns[0]["example"]
    assert isinstance(example, dict)
    # The example must actually demonstrate command-tuple syntax (a list
    # whose elements are themselves 2- or 3-element lists), not a flat
    # id list — that's the whole point of the example.
    tag_ids = example["values"]["tag_ids"]
    assert isinstance(tag_ids, list) and tag_ids
    assert all(isinstance(cmd, list) and len(cmd) in (2, 3) for cmd in tag_ids)


def test_help_verbose_documents_many2one_id_rule(tmp_path: Path) -> None:
    """The cookbook must say many2one writes take an integer id, not a name.

    Pinned because the rule is non-obvious to agents that have just used
    odoo_lookup or odoo_search_read (where names appear everywhere) and
    then try to write the name back.
    """
    app = _build_app(tmp_path)
    dispatcher = Dispatcher(app)

    payload = _call(dispatcher, "odoo_help", {"verbose": True})
    gotchas = payload["gotchas"]
    assert isinstance(gotchas, list)
    blob = "\n".join(g for g in gotchas if isinstance(g, str))
    assert "many2one" in blob.lower()
    assert "integer id" in blob.lower() or "integer ID" in blob
    # And the corresponding common-patterns entry must point at odoo_lookup
    # as the resolver for name -> id.
    patterns = payload["common_patterns"]
    m2o_patterns = [p for p in patterns if isinstance(p, dict) and "many2one" in p.get("goal", "")]
    assert m2o_patterns, "expected at least one many2one write pattern"
    assert any("odoo_lookup" in (p.get("use", "") + str(p.get("example", ""))) for p in m2o_patterns)
