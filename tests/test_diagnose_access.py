"""Tests for the odoo_diagnose_access tool."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from odoo_mcp.audit import AuditLog
from odoo_mcp.client import OdooClient
from odoo_mcp.config import AppConfig, Defaults, InstanceConfig
from odoo_mcp.credentials import Credentials
from odoo_mcp.dispatcher import Dispatcher, InstanceRuntime, OdooMcpApp
from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD
from odoo_mcp.security.limits import RateLimiter
from odoo_mcp.security.prod_guard import ProdGuard
from odoo_mcp.tools import build_tools


class _FakeClient:
    def __init__(
        self, rights: dict[str, bool] | None = None, *, is_admin: bool | None = False
    ) -> None:
        self._rights = rights or {
            "read": True,
            "write": False,
            "create": False,
            "unlink": False,
        }
        self.is_admin = is_admin
        self.admin_reason: str | None = None
        self.uid = 7
        self.username: str | None = "alice"
        self.calls: list[tuple[str, str]] = []

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        return {"id": {"type": "integer"}, "name": {"type": "char"}}

    def check_access_rights(self, model: str, op: str) -> bool:
        self.calls.append((model, op))
        return self._rights.get(op, False)


def _instance_config(production: bool = False) -> InstanceConfig:
    return InstanceConfig(
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


def _build(tmp_path: Path, fake: _FakeClient) -> OdooMcpApp:
    cfg = _instance_config()
    creds = Credentials(instance_name=cfg.name, username="u", _api_key="k" * 10)
    real = OdooClient(cfg, credentials=creds)
    app_cfg = AppConfig(
        path=tmp_path / "config.toml",
        defaults=Defaults(),
        instances={cfg.name: cfg},
        audit_log_path=tmp_path / "audit.jsonl",
    )
    audit = AuditLog(app_cfg.audit_log_path)
    rl = RateLimiter()
    rl.configure(cfg.name, cfg.rate_limit_per_minute)
    rt = InstanceRuntime(config=cfg, client=real)
    rt.client = fake  # type: ignore[assignment]
    return OdooMcpApp(
        config=app_cfg,
        audit=audit,
        prod_guard=ProdGuard(),
        rate_limiter=rl,
        instances={cfg.name: rt},
    )


def _call(disp: Dispatcher, args: dict[str, Any]) -> dict[str, Any]:
    contents = asyncio.run(disp.call("odoo_diagnose_access", args))
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


def test_diagnose_access_registered() -> None:
    names = [t.name for t in build_tools()]
    assert "odoo_diagnose_access" in names


def test_diagnose_access_returns_four_rights(tmp_path: Path) -> None:
    fake = _FakeClient({"read": True, "write": True, "create": False, "unlink": False})
    app = _build(tmp_path, fake)
    payload = _call(Dispatcher(app), {"instance": "dev", "model": "res.partner"})
    assert payload["ok"] is True
    assert payload["can_read"] is True
    assert payload["can_write"] is True
    assert payload["can_create"] is False
    assert payload["can_unlink"] is False
    assert payload["uid"] == 7
    assert payload["login"] == "alice"
    assert payload["model"] == "res.partner"


def test_diagnose_access_calls_check_for_each_op(tmp_path: Path) -> None:
    fake = _FakeClient()
    app = _build(tmp_path, fake)
    _call(Dispatcher(app), {"instance": "dev", "model": "res.partner"})
    ops = [op for _, op in fake.calls]
    assert sorted(ops) == ["create", "read", "unlink", "write"]


def test_diagnose_access_reports_denied_model_instead_of_failing(tmp_path: Path) -> None:
    """The whole point of the tool is explaining access state — a model
    blocked by MCP policy must come back as a structured report (with the
    reason and the config key), not as the same error the caller is
    trying to diagnose. No Odoo RPC happens for blocked models."""
    fake = _FakeClient()
    app = _build(tmp_path, fake)
    payload = _call(Dispatcher(app), {"instance": "dev", "model": "res.groups"})
    assert payload["ok"] is True
    assert payload["mcp_blocked"] is True
    assert "denylist" in payload["mcp_block_reason"].lower()
    assert "allowed_models" in payload["note"]
    assert fake.calls == []


def test_diagnose_access_flags_write_blocklist(tmp_path: Path) -> None:
    """res.users is readable (field-whitelisted) but the MCP refuses all
    writes — the report must say so even when Odoo ACLs allow writing."""
    fake = _FakeClient()
    app = _build(tmp_path, fake)
    payload = _call(Dispatcher(app), {"instance": "dev", "model": "res.users"})
    assert payload["ok"] is True
    assert payload["mcp_blocked"] is False
    assert payload["write_blocked_via_mcp"] is True


def test_diagnose_access_malformed_model_name_still_fails(tmp_path: Path) -> None:
    fake = _FakeClient()
    app = _build(tmp_path, fake)
    payload = _call(Dispatcher(app), {"instance": "dev", "model": "res partner;drop"})
    assert payload["ok"] is False
    assert "invalid characters" in payload["error"]
    assert fake.calls == []


def test_diagnose_access_admin_flag_propagated(tmp_path: Path) -> None:
    fake = _FakeClient(is_admin=True)
    fake.admin_reason = "superuser (uid=1, OdooBot)"
    app = _build(tmp_path, fake)
    payload = _call(Dispatcher(app), {"instance": "dev", "model": "res.partner"})
    assert payload["is_admin"] is True
    assert "superuser" in payload["admin_reason"]
