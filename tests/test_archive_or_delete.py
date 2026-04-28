"""Tests for the odoo_archive_or_delete tool.

Covers mode validation, the 'active' field precondition for archive,
id-count caps, dry-run preview with reminder text, prod confirmation token
flow, and audit-log shape.
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
    """Mimics the subset of OdooClient the dispatcher uses.

    Records calls so tests can assert on write vs unlink dispatch.
    """

    def __init__(self, fields: dict[str, dict[str, Any]] | None = None) -> None:
        self._fields = fields if fields is not None else {"id": {"type": "integer"}}
        self.write_calls: list[tuple[str, list[int], dict[str, Any]]] = []
        self.unlink_calls: list[tuple[str, list[int]]] = []
        # Match the OdooClient interface used by _instance_summary.
        self.is_admin: bool | None = None
        self.admin_reason: str | None = None

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        return self._fields

    def write(self, model: str, ids: list[int], values: dict[str, Any]) -> bool:
        self.write_calls.append((model, ids, values))
        return True

    def unlink(self, model: str, ids: list[int]) -> bool:
        self.unlink_calls.append((model, ids))
        return True


def _instance_config(name: str, production: bool) -> InstanceConfig:
    return InstanceConfig(
        name=name,
        url="https://example.odoo.com",
        database="db",
        credentials_env_prefix=f"ODOO_MCP_{name.upper()}",
        production=production,
        timeout_seconds=30,
        max_records_default=50,
        max_records_hard_cap=500,
        rate_limit_per_minute=300,
        allow_self_signed=False,
        allowed_models=frozenset({ALLOWLIST_WILDCARD}),
    )


def _build_app(
    tmp_path: Path,
    *,
    production: bool = False,
    fields: dict[str, dict[str, Any]] | None = None,
) -> tuple[OdooMcpApp, _FakeClient]:
    inst_cfg = _instance_config("prod" if production else "dev", production)
    # Construct a real OdooClient to satisfy InstanceRuntime's type, then
    # swap it out. The constructor doesn't contact Odoo.
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
    contents = asyncio.run(dispatcher.call("odoo_archive_or_delete", args))
    assert len(contents) == 1
    payload: dict[str, Any] = json.loads(contents[0].text)
    return payload


# -- Schema --------------------------------------------------------------------


def test_archive_or_delete_registered_as_tool() -> None:
    names = [t.name for t in build_tools()]
    assert "odoo_archive_or_delete" in names


def test_tool_description_mentions_archive_first() -> None:
    tool = next(t for t in build_tools() if t.name == "odoo_archive_or_delete")
    desc = tool.description or ""
    assert "archive" in desc.lower()
    assert "permanent" in desc.lower()
    # The model should be reminded to ASK the user first.
    assert "ask the user" in desc.lower()


def test_help_gotchas_mention_archive_or_delete(tmp_path: Path) -> None:
    app, _ = _build_app(tmp_path)
    dispatcher = Dispatcher(app)
    contents = asyncio.run(dispatcher.call("odoo_help", {}))
    payload = json.loads(contents[0].text)
    gotchas = payload["gotchas"]
    assert any("archive" in g.lower() for g in gotchas)


# -- Validation ---------------------------------------------------------------


def test_invalid_mode_raises(tmp_path: Path) -> None:
    app, _ = _build_app(tmp_path)
    dispatcher = Dispatcher(app)
    payload = _call(
        dispatcher,
        {"instance": "dev", "model": "res.partner", "ids": [1], "mode": "nuke"},
    )
    assert payload["ok"] is False
    assert "mode must be" in payload["error"]


def test_archive_requires_active_field(tmp_path: Path) -> None:
    # Model with no 'active' field.
    fields = {"id": {"type": "integer"}, "name": {"type": "char"}}
    app, _ = _build_app(tmp_path, fields=fields)
    dispatcher = Dispatcher(app)
    payload = _call(
        dispatcher,
        {"instance": "dev", "model": "some.model", "ids": [1], "mode": "archive"},
    )
    assert payload["ok"] is False
    assert payload["error_code"] == "field_policy"
    assert "no 'active' field" in payload["error"]


def test_delete_does_not_require_active_field(tmp_path: Path) -> None:
    fields = {"id": {"type": "integer"}}
    app, fake = _build_app(tmp_path, fields=fields)
    dispatcher = Dispatcher(app)
    # On dev, effective dry-run is False by default -> actual unlink call.
    payload = _call(
        dispatcher,
        {"instance": "dev", "model": "some.model", "ids": [1], "mode": "delete"},
    )
    assert payload["ok"] is True
    assert payload["committed"] is True
    assert fake.unlink_calls == [("some.model", [1])]


def test_id_count_cap_enforced(tmp_path: Path) -> None:
    app, _ = _build_app(tmp_path)
    dispatcher = Dispatcher(app)
    too_many = list(range(1, 502))  # exceeds max_records_hard_cap=500
    payload = _call(
        dispatcher,
        {
            "instance": "dev",
            "model": "res.partner",
            "ids": too_many,
            "mode": "delete",
        },
    )
    assert payload["ok"] is False
    assert "more than 500" in payload["error"]


# -- Dry-run behavior ---------------------------------------------------------


def test_dry_run_archive_returns_preview_with_reversible_reminder(
    tmp_path: Path,
) -> None:
    fields = {"id": {"type": "integer"}, "active": {"type": "boolean"}}
    app, fake = _build_app(tmp_path, fields=fields)
    dispatcher = Dispatcher(app)
    payload = _call(
        dispatcher,
        {
            "instance": "dev",
            "model": "res.partner",
            "ids": [1, 2, 3],
            "mode": "archive",
            "dry_run": True,
        },
    )
    assert payload["ok"] is True
    assert payload["preview"] is True
    assert payload["mode"] == "archive"
    assert payload["id_count"] == 3
    assert payload["confirmation_token"].startswith("conf_")
    assert "reversible" in payload["reminder"].lower()
    # Nothing was actually written.
    assert fake.write_calls == []
    assert fake.unlink_calls == []


def test_dry_run_delete_returns_preview_with_permanent_warning(
    tmp_path: Path,
) -> None:
    app, fake = _build_app(tmp_path)
    dispatcher = Dispatcher(app)
    payload = _call(
        dispatcher,
        {
            "instance": "dev",
            "model": "res.partner",
            "ids": [42],
            "mode": "delete",
            "dry_run": True,
        },
    )
    assert payload["ok"] is True
    assert payload["preview"] is True
    assert payload["mode"] == "delete"
    reminder = payload["reminder"]
    assert "PERMANENT" in reminder
    assert "archiving" in reminder.lower()
    assert fake.unlink_calls == []


# -- Commit paths -------------------------------------------------------------


def test_archive_commit_writes_active_false(tmp_path: Path) -> None:
    fields = {"id": {"type": "integer"}, "active": {"type": "boolean"}}
    app, fake = _build_app(tmp_path, fields=fields)
    dispatcher = Dispatcher(app)
    payload = _call(
        dispatcher,
        {
            "instance": "dev",
            "model": "res.partner",
            "ids": [7, 8],
            "mode": "archive",
            "dry_run": False,
        },
    )
    assert payload["ok"] is True
    assert payload["committed"] is True
    assert fake.write_calls == [("res.partner", [7, 8], {"active": False})]
    assert fake.unlink_calls == []


def test_delete_commit_calls_unlink(tmp_path: Path) -> None:
    app, fake = _build_app(tmp_path)
    dispatcher = Dispatcher(app)
    payload = _call(
        dispatcher,
        {
            "instance": "dev",
            "model": "res.partner",
            "ids": [9],
            "mode": "delete",
            "dry_run": False,
        },
    )
    assert payload["ok"] is True
    assert payload["committed"] is True
    assert fake.unlink_calls == [("res.partner", [9])]
    assert fake.write_calls == []


# -- Prod-guard flow ----------------------------------------------------------


def test_prod_commit_requires_unlock_and_token(tmp_path: Path) -> None:
    fields = {"id": {"type": "integer"}, "active": {"type": "boolean"}}
    app, fake = _build_app(tmp_path, production=True, fields=fields)
    dispatcher = Dispatcher(app)

    # 1) Without unlock, the write gate blocks the call.
    payload = _call(
        dispatcher,
        {
            "instance": "prod",
            "model": "res.partner",
            "ids": [1],
            "mode": "archive",
        },
    )
    assert payload["ok"] is False
    assert payload["error_code"] == "prod_guard"

    # 2) Unlock, then dry-run returns a token.
    app.prod_guard.unlock("prod", production=True)
    preview = _call(
        dispatcher,
        {
            "instance": "prod",
            "model": "res.partner",
            "ids": [1],
            "mode": "archive",
        },
    )
    assert preview["ok"] is True
    assert preview["preview"] is True
    token = preview["confirmation_token"]

    # 3) Commit without the token is refused.
    no_token = _call(
        dispatcher,
        {
            "instance": "prod",
            "model": "res.partner",
            "ids": [1],
            "mode": "archive",
            "dry_run": False,
        },
    )
    assert no_token["ok"] is False
    assert no_token["error_code"] == "prod_guard"
    assert fake.write_calls == []

    # 4) Commit with the token succeeds.
    committed = _call(
        dispatcher,
        {
            "instance": "prod",
            "model": "res.partner",
            "ids": [1],
            "mode": "archive",
            "dry_run": False,
            "confirmation_token": token,
        },
    )
    assert committed["ok"] is True
    assert committed["committed"] is True
    assert fake.write_calls == [("res.partner", [1], {"active": False})]


# -- Audit --------------------------------------------------------------------


def test_audit_entry_contains_mode(tmp_path: Path) -> None:
    app, _ = _build_app(tmp_path)
    dispatcher = Dispatcher(app)
    _call(
        dispatcher,
        {
            "instance": "dev",
            "model": "res.partner",
            "ids": [1],
            "mode": "delete",
            "dry_run": False,
        },
    )
    log_lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    # Most recent event is our call.
    event = json.loads(log_lines[-1])
    assert event["tool"] == "odoo_archive_or_delete"
    assert event["op"] == "unlink"
    details = event["details"]
    # The mode is recorded in details for the unlink call.
    assert details.get("mode") == "delete"
    # The args-shape block also carries the scalar mode string.
    assert details["args"]["mode"] == "delete"
