"""Tests for the odoo_lookup tool.

Covers domain construction, field shape of the result, limit clamping,
denylist enforcement, and audit-log shape.
"""

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
    def __init__(self, fields: dict[str, dict[str, Any]] | None = None) -> None:
        self._fields = (
            fields
            if fields is not None
            else {
                "id": {"type": "integer"},
                "name": {"type": "char"},
                "display_name": {"type": "char"},
            }
        )
        self.lookup_calls: list[tuple[str, str, int]] = []
        self.next_results: list[dict[str, Any]] = []
        self.is_admin: bool | None = None
        self.admin_reason: str | None = None

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        return self._fields

    def lookup(self, model: str, query: str, limit: int) -> list[dict[str, Any]]:
        self.lookup_calls.append((model, query, limit))
        return list(self.next_results)


def _instance_config(name: str = "dev", hard_cap: int = 500) -> InstanceConfig:
    return InstanceConfig(
        name=name,
        url="https://example.odoo.com",
        database="db",
        credentials_env_prefix=f"ODOO_MCP_{name.upper()}",
        production=False,
        timeout_seconds=30,
        max_records_default=50,
        max_records_hard_cap=hard_cap,
        rate_limit_per_minute=300,
        allow_self_signed=False,
        allowed_models=frozenset({ALLOWLIST_WILDCARD}),
    )


def _build_app(
    tmp_path: Path,
    *,
    fields: dict[str, dict[str, Any]] | None = None,
    hard_cap: int = 500,
) -> tuple[OdooMcpApp, _FakeClient]:
    inst_cfg = _instance_config(hard_cap=hard_cap)
    creds = Credentials(instance_name=inst_cfg.name, username="u", _api_key="k" * 10)
    real_client = OdooClient(inst_cfg, credentials=creds)
    fake = _FakeClient(fields=fields)
    app_cfg = AppConfig(
        path=tmp_path / "config.toml",
        defaults=Defaults(),
        instances={inst_cfg.name: inst_cfg},
        audit_log_path=tmp_path / "audit.jsonl",
    )
    audit = AuditLog(app_cfg.audit_log_path)
    rate_limiter = RateLimiter()
    rate_limiter.configure(inst_cfg.name, inst_cfg.rate_limit_per_minute)
    rt = InstanceRuntime(config=inst_cfg, client=real_client)
    rt.client = fake  # type: ignore[assignment]
    app = OdooMcpApp(
        config=app_cfg,
        audit=audit,
        prod_guard=ProdGuard(),
        rate_limiter=rate_limiter,
        instances={inst_cfg.name: rt},
    )
    return app, fake


def _call(dispatcher: Dispatcher, args: dict[str, Any]) -> dict[str, Any]:
    contents = asyncio.run(dispatcher.call("odoo_lookup", args))
    assert len(contents) == 1
    payload: dict[str, Any] = json.loads(contents[0].text)
    return payload


# -- Schema -------------------------------------------------------------------


def test_lookup_registered_as_tool() -> None:
    names = [t.name for t in build_tools()]
    assert "odoo_lookup" in names


# -- Behavior -----------------------------------------------------------------


def test_lookup_calls_search_read_with_ilike(tmp_path: Path) -> None:
    app, fake = _build_app(tmp_path)
    fake.next_results = [{"id": 1, "display_name": "Acme Inc"}]
    dispatcher = Dispatcher(app)
    payload = _call(
        dispatcher,
        {"instance": "dev", "model": "res.partner", "query": "Acme"},
    )
    assert payload["ok"] is True
    assert fake.lookup_calls == [("res.partner", "Acme", 10)]


def test_lookup_returns_id_and_display_name_only(tmp_path: Path) -> None:
    app, fake = _build_app(tmp_path)
    fake.next_results = [
        {"id": 1, "display_name": "Acme Inc"},
        {"id": 2, "display_name": "Acme Subsidiary"},
    ]
    dispatcher = Dispatcher(app)
    payload = _call(
        dispatcher,
        {"instance": "dev", "model": "res.partner", "query": "Acme", "limit": 5},
    )
    assert payload["ok"] is True
    assert payload["count"] == 2
    assert payload["results"] == [
        {"id": 1, "display_name": "Acme Inc"},
        {"id": 2, "display_name": "Acme Subsidiary"},
    ]


def test_lookup_clamps_limit_to_hard_cap(tmp_path: Path) -> None:
    app, fake = _build_app(tmp_path, hard_cap=20)
    fake.next_results = []
    dispatcher = Dispatcher(app)
    _call(
        dispatcher,
        {"instance": "dev", "model": "res.partner", "query": "Acme", "limit": 1000},
    )
    assert fake.lookup_calls[-1][2] == 20


def test_lookup_returns_empty_list_when_no_matches(tmp_path: Path) -> None:
    app, fake = _build_app(tmp_path)
    fake.next_results = []
    dispatcher = Dispatcher(app)
    payload = _call(
        dispatcher,
        {"instance": "dev", "model": "res.partner", "query": "NoMatch"},
    )
    assert payload["ok"] is True
    assert payload["count"] == 0
    assert payload["results"] == []


def test_lookup_blocked_by_denylist(tmp_path: Path) -> None:
    app, _ = _build_app(tmp_path)
    dispatcher = Dispatcher(app)
    payload = _call(
        dispatcher,
        {"instance": "dev", "model": "res.groups", "query": "admin"},
    )
    assert payload["ok"] is False
    assert payload["error_code"] == "model_not_allowed"


def test_lookup_res_users_allowed_for_identity_resolution(tmp_path: Path) -> None:
    """res.users left the denylist in v0.22.0 — resolving a salesperson
    user_id to a name is the most common relational lookup in CRM flows.
    Reads expose only the identity-field whitelist; writes stay blocked."""
    app, fake = _build_app(tmp_path)
    fake.next_results = [{"id": 7, "display_name": "Alice"}]
    dispatcher = Dispatcher(app)
    payload = _call(
        dispatcher,
        {"instance": "dev", "model": "res.users", "query": "alice"},
    )
    assert payload["ok"] is True
    assert payload["results"] == [{"id": 7, "display_name": "Alice"}]


def test_lookup_audit_records_op(tmp_path: Path) -> None:
    app, fake = _build_app(tmp_path)
    fake.next_results = [{"id": 1, "display_name": "Acme"}]
    dispatcher = Dispatcher(app)
    _call(
        dispatcher,
        {"instance": "dev", "model": "res.partner", "query": "Acme"},
    )
    log_lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    event = json.loads(log_lines[-1])
    assert event["tool"] == "odoo_lookup"
    assert event["op"] == "lookup"
    details = event["details"]
    assert details.get("query_len") == 4
    assert details.get("result_count") == 1
    # The query value must NEVER appear in the audit log.
    assert "Acme" not in json.dumps(event)


def test_lookup_default_limit_is_ten(tmp_path: Path) -> None:
    app, fake = _build_app(tmp_path)
    fake.next_results = []
    dispatcher = Dispatcher(app)
    _call(dispatcher, {"instance": "dev", "model": "res.partner", "query": "x"})
    assert fake.lookup_calls[-1][2] == 10


def test_lookup_in_help_patterns(tmp_path: Path) -> None:
    app, _ = _build_app(tmp_path)
    dispatcher = Dispatcher(app)
    # common_patterns lives in verbose mode (v0.11.0+).
    contents = asyncio.run(dispatcher.call("odoo_help", {"verbose": True}))
    payload = json.loads(contents[0].text)
    patterns = payload["common_patterns"]
    assert any("odoo_lookup" in (p.get("use") or "") for p in patterns)
