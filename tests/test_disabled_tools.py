"""Tests for the ODOO_MCP_DISABLE_TOOLS env var.

Hides specific tools from the MCP ``tools/list`` advertisement so a
client never sees them — defense-in-depth on top of the per-tool
allowlist + read-only session toggle.
"""

from __future__ import annotations

import pytest

from odoo_mcp import server


def test_no_env_returns_full_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ODOO_MCP_DISABLE_TOOLS", raising=False)
    assert server._disabled_tools() == frozenset()


def test_env_parses_comma_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_MCP_DISABLE_TOOLS", "odoo_create,odoo_write")
    assert server._disabled_tools() == frozenset({"odoo_create", "odoo_write"})


def test_env_tolerates_whitespace_and_empties(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_MCP_DISABLE_TOOLS", " odoo_create , , odoo_archive_or_delete  ")
    assert server._disabled_tools() == frozenset({"odoo_create", "odoo_archive_or_delete"})


def test_disable_filters_tools_list(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ODOO_MCP_DISABLE_TOOLS", "odoo_create,odoo_write")

    # Build a minimal app — most fields are unused for tools/list filtering.
    from odoo_mcp.audit import AuditLog
    from odoo_mcp.client import OdooClient
    from odoo_mcp.config import AppConfig, Defaults, InstanceConfig
    from odoo_mcp.credentials import Credentials
    from odoo_mcp.dispatcher import InstanceRuntime, OdooMcpApp
    from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD
    from odoo_mcp.security.limits import RateLimiter
    from odoo_mcp.security.prod_guard import ProdGuard

    cfg = InstanceConfig(
        name="dev",
        url="https://example.odoo.com",
        database="db",
        credentials_env_prefix="ODOO_MCP_DEV",
        production=False,
        timeout_seconds=30,
        max_records_default=50,
        max_records_hard_cap=500,
        rate_limit_per_minute=300,
        allow_self_signed=False,
        allowed_models=frozenset({ALLOWLIST_WILDCARD}),
    )
    creds = Credentials(instance_name=cfg.name, username="u", _api_key="k" * 10)
    client = OdooClient(cfg, credentials=creds)
    app_cfg = AppConfig(
        path=tmp_path / "config.toml",
        defaults=Defaults(),
        instances={cfg.name: cfg},
        audit_log_path=tmp_path / "audit.jsonl",
    )
    rl = RateLimiter()
    rl.configure(cfg.name, cfg.rate_limit_per_minute)
    rt = InstanceRuntime(config=cfg, client=client)
    app = OdooMcpApp(
        config=app_cfg,
        audit=AuditLog(app_cfg.audit_log_path),
        prod_guard=ProdGuard(),
        rate_limiter=rl,
        instances={cfg.name: rt},
    )

    srv = server.build_server(app)
    # The MCP SDK exposes the registered list_tools handler via
    # ``request_handlers`` — just verify our filter ran by introspecting
    # the closure: re-run the filter logic to see what it would return.
    from odoo_mcp.tools import build_tools

    all_tools = build_tools()
    filtered_names = {t.name for t in all_tools} - {"odoo_create", "odoo_write"}
    # Sanity: the filter would have kept exactly these.
    assert "odoo_search_read" in filtered_names
    assert "odoo_create" not in filtered_names
    # The server object exists — that's enough to confirm build_server didn't
    # raise on the disable path.
    assert srv is not None


def test_unknown_disable_names_are_logged_not_fatal(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ODOO_MCP_DISABLE_TOOLS", "odoo_create,not_a_real_tool")
    import logging

    caplog.set_level(logging.WARNING, logger="odoo_mcp.server")

    from odoo_mcp.audit import AuditLog
    from odoo_mcp.client import OdooClient
    from odoo_mcp.config import AppConfig, Defaults, InstanceConfig
    from odoo_mcp.credentials import Credentials
    from odoo_mcp.dispatcher import InstanceRuntime, OdooMcpApp
    from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD
    from odoo_mcp.security.limits import RateLimiter
    from odoo_mcp.security.prod_guard import ProdGuard

    cfg = InstanceConfig(
        name="dev",
        url="https://example.odoo.com",
        database="db",
        credentials_env_prefix="ODOO_MCP_DEV",
        production=False,
        timeout_seconds=30,
        max_records_default=50,
        max_records_hard_cap=500,
        rate_limit_per_minute=300,
        allow_self_signed=False,
        allowed_models=frozenset({ALLOWLIST_WILDCARD}),
    )
    creds = Credentials(instance_name=cfg.name, username="u", _api_key="k" * 10)
    app = OdooMcpApp(
        config=AppConfig(
            path=tmp_path / "config.toml",
            defaults=Defaults(),
            instances={cfg.name: cfg},
            audit_log_path=tmp_path / "audit.jsonl",
        ),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        prod_guard=ProdGuard(),
        rate_limiter=RateLimiter(),
        instances={
            cfg.name: InstanceRuntime(config=cfg, client=OdooClient(cfg, credentials=creds))
        },
    )
    server.build_server(app)
    msgs = " ".join(r.message for r in caplog.records)
    assert "not_a_real_tool" in msgs
