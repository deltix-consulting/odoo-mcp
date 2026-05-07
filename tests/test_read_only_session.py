"""Tests for the ODOO_MCP_READ_ONLY session toggle.

When set to a truthy value, every write-path tool refuses regardless of
per-instance ``production`` flags or unlock state. Reads remain
unaffected.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from odoo_mcp.audit import AuditLog
from odoo_mcp.client import OdooClient
from odoo_mcp.config import AppConfig, Defaults, InstanceConfig
from odoo_mcp.credentials import Credentials
from odoo_mcp.dispatcher import Dispatcher, InstanceRuntime, OdooMcpApp
from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD
from odoo_mcp.security.limits import RateLimiter
from odoo_mcp.security.prod_guard import ProdGuard


class _FakeClient:
    def __init__(self) -> None:
        self.is_admin: bool | None = None
        self.admin_reason: str | None = None
        self.username = "u"
        self.uid = 1

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        return {"id": {"type": "integer"}, "name": {"type": "char", "string": "Name"}}

    def search_read(self, *_a: Any, **_kw: Any) -> list[dict[str, Any]]:
        return []

    def create(self, model: str, values: dict[str, Any]) -> int:
        return 99

    def write(self, model: str, ids: list[int], values: dict[str, Any]) -> bool:
        return True


def _build(tmp_path: Path, *, production: bool = False) -> OdooMcpApp:
    cfg = InstanceConfig(
        name="dev",
        url="https://example.odoo.com",
        database="db",
        credentials_env_prefix="ODOO_MCP_DEV",
        production=production,
        timeout_seconds=30,
        max_records_default=50,
        max_records_hard_cap=500,
        rate_limit_per_minute=300,
        allow_self_signed=False,
        allowed_models=frozenset({ALLOWLIST_WILDCARD}),
    )
    creds = Credentials(instance_name=cfg.name, username="u", _api_key="k" * 10)
    real = OdooClient(cfg, credentials=creds)
    app_cfg = AppConfig(
        path=tmp_path / "config.toml",
        defaults=Defaults(),
        instances={cfg.name: cfg},
        audit_log_path=tmp_path / "audit.jsonl",
    )
    rl = RateLimiter()
    rl.configure(cfg.name, cfg.rate_limit_per_minute)
    rt = InstanceRuntime(config=cfg, client=real)
    rt.client = _FakeClient()  # type: ignore[assignment]
    return OdooMcpApp(
        config=app_cfg,
        audit=AuditLog(app_cfg.audit_log_path),
        prod_guard=ProdGuard(),
        rate_limiter=rl,
        instances={cfg.name: rt},
    )


def _call(disp: Dispatcher, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    contents = asyncio.run(disp.call(tool, args))
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


@pytest.fixture
def read_only_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_MCP_READ_ONLY", "1")


def test_read_only_blocks_create(tmp_path: Path, read_only_env: None) -> None:
    app = _build(tmp_path)
    payload = _call(
        Dispatcher(app),
        "odoo_create",
        {"instance": "dev", "model": "res.partner", "values": {"name": "X"}},
    )
    assert payload["ok"] is False
    assert "read-only" in payload["error"].lower()


def test_read_only_blocks_write(tmp_path: Path, read_only_env: None) -> None:
    app = _build(tmp_path)
    payload = _call(
        Dispatcher(app),
        "odoo_write",
        {
            "instance": "dev",
            "model": "res.partner",
            "ids": [1],
            "values": {"name": "X"},
        },
    )
    assert payload["ok"] is False
    assert "read-only" in payload["error"].lower()


def test_read_only_blocks_archive_or_delete(tmp_path: Path, read_only_env: None) -> None:
    app = _build(tmp_path)
    payload = _call(
        Dispatcher(app),
        "odoo_archive_or_delete",
        {"instance": "dev", "model": "res.partner", "ids": [1], "mode": "archive"},
    )
    assert payload["ok"] is False
    assert "read-only" in payload["error"].lower()


def test_read_only_blocks_enable_prod_writes(tmp_path: Path, read_only_env: None) -> None:
    app = _build(tmp_path, production=True)
    payload = _call(
        Dispatcher(app), "odoo_enable_prod_writes", {"instance": "dev"}
    )
    assert payload["ok"] is False
    assert "read-only" in payload["error"].lower()


def test_read_only_does_not_block_reads(tmp_path: Path, read_only_env: None) -> None:
    app = _build(tmp_path)
    payload = _call(
        Dispatcher(app),
        "odoo_search_read",
        {"instance": "dev", "model": "res.partner", "fields": ["id", "name"]},
    )
    assert payload["ok"] is True


def test_read_only_surfaces_in_list_instances(tmp_path: Path, read_only_env: None) -> None:
    app = _build(tmp_path)
    payload = _call(Dispatcher(app), "odoo_list_instances", {})
    assert payload.get("session_read_only") is True


def test_no_env_var_means_writes_allowed(tmp_path: Path) -> None:
    app = _build(tmp_path)
    payload = _call(
        Dispatcher(app),
        "odoo_create",
        {"instance": "dev", "model": "res.partner", "values": {"name": "X"}},
    )
    assert payload["ok"] is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "garbage"])
def test_falsy_env_values_do_not_enable_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("ODOO_MCP_READ_ONLY", value)
    app = _build(tmp_path)
    payload = _call(
        Dispatcher(app),
        "odoo_create",
        {"instance": "dev", "model": "res.partner", "values": {"name": "X"}},
    )
    # Falsy / unknown values must not block writes — only explicit truthy
    # tokens flip the gate.
    assert payload["ok"] is True


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on"])
def test_truthy_env_values_enable_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("ODOO_MCP_READ_ONLY", value)
    app = _build(tmp_path)
    payload = _call(
        Dispatcher(app),
        "odoo_create",
        {"instance": "dev", "model": "res.partner", "values": {"name": "X"}},
    )
    assert payload["ok"] is False
