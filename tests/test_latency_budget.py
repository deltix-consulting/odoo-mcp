"""Tests for the ODOO_MCP_TOOL_LATENCY_BUDGET_MS observability hook."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from odoo_mcp import dispatcher


def test_unset_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ODOO_MCP_TOOL_LATENCY_BUDGET_MS", raising=False)
    assert dispatcher._latency_budget_ms() is None


def test_empty_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_MCP_TOOL_LATENCY_BUDGET_MS", "")
    assert dispatcher._latency_budget_ms() is None


def test_zero_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_MCP_TOOL_LATENCY_BUDGET_MS", "0")
    assert dispatcher._latency_budget_ms() is None


def test_negative_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_MCP_TOOL_LATENCY_BUDGET_MS", "-100")
    assert dispatcher._latency_budget_ms() is None


def test_garbage_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_MCP_TOOL_LATENCY_BUDGET_MS", "fast")
    assert dispatcher._latency_budget_ms() is None


def test_positive_returns_int(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_MCP_TOOL_LATENCY_BUDGET_MS", "1500")
    assert dispatcher._latency_budget_ms() == 1500


def test_warning_logged_on_overrun(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """When a tool call's elapsed time exceeds the budget, a WARNING fires.

    We don't drive a real tool here — we sanity-check the log line by
    forcing ``_elapsed_ms`` to return a high value and stubbing a
    minimal call into the dispatcher.
    """
    monkeypatch.setenv("ODOO_MCP_TOOL_LATENCY_BUDGET_MS", "10")

    # Patch _elapsed_ms to claim 250ms regardless of real timing.
    monkeypatch.setattr(dispatcher, "_elapsed_ms", lambda _started: 250)

    # Build a tiny app with one fake instance and call odoo_help (no Odoo
    # round trip). Replicates the wiring in tests/test_help.py minimally.
    import asyncio
    from pathlib import Path

    from odoo_mcp.audit import AuditLog
    from odoo_mcp.client import OdooClient
    from odoo_mcp.config import AppConfig, Defaults, InstanceConfig
    from odoo_mcp.credentials import Credentials
    from odoo_mcp.dispatcher import Dispatcher, InstanceRuntime, OdooMcpApp
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
    tmp = Path(".")
    app_cfg = AppConfig(
        path=tmp / "config.toml",
        defaults=Defaults(),
        instances={cfg.name: cfg},
        audit_log_path=tmp / "audit.jsonl",
    )
    app = OdooMcpApp(
        config=app_cfg,
        audit=AuditLog(app_cfg.audit_log_path),
        prod_guard=ProdGuard(),
        rate_limiter=RateLimiter(),
        instances={cfg.name: InstanceRuntime(config=cfg, client=client)},
    )

    caplog.set_level(logging.WARNING, logger="odoo_mcp.dispatcher")
    asyncio.run(Dispatcher(app).call("odoo_help", {}))
    msgs = " ".join(r.message for r in caplog.records)
    assert "slow_tool_call" in msgs
    assert "elapsed_ms=250" in msgs
    assert "budget_ms=10" in msgs


def test_no_warning_when_below_budget(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Any
) -> None:
    monkeypatch.setenv("ODOO_MCP_TOOL_LATENCY_BUDGET_MS", "1000")
    monkeypatch.setattr(dispatcher, "_elapsed_ms", lambda _started: 5)

    import asyncio

    from odoo_mcp.audit import AuditLog
    from odoo_mcp.client import OdooClient
    from odoo_mcp.config import AppConfig, Defaults, InstanceConfig
    from odoo_mcp.credentials import Credentials
    from odoo_mcp.dispatcher import Dispatcher, InstanceRuntime, OdooMcpApp
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
    app = OdooMcpApp(
        config=app_cfg,
        audit=AuditLog(app_cfg.audit_log_path),
        prod_guard=ProdGuard(),
        rate_limiter=RateLimiter(),
        instances={cfg.name: InstanceRuntime(config=cfg, client=client)},
    )

    caplog.set_level(logging.WARNING, logger="odoo_mcp.dispatcher")
    asyncio.run(Dispatcher(app).call("odoo_help", {}))
    msgs = " ".join(r.message for r in caplog.records)
    assert "slow_tool_call" not in msgs
