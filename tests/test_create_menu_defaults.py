"""odoo_create says when a record-kind field falls back to Odoo's default.

The web menus open a form with a ``default_<field>`` context key (Invoicing >
Invoices sends ``default_move_type: 'out_invoice'``). deltix's context is
frozen, so an RPC create without ``move_type`` makes a Journal Entry, without
``payment_type`` an inbound payment, without ``type`` on crm.lead possibly a
lead — and the result says ``committed: true``. The result must say so.
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
from odoo_mcp.dispatcher import _MENU_CONTEXT_DEFAULTS, Dispatcher, InstanceRuntime, OdooMcpApp
from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD
from odoo_mcp.security.limits import RateLimiter
from odoo_mcp.security.prod_guard import ProdGuard

_META: dict[str, dict[str, dict[str, Any]]] = {
    "account.move": {
        "id": {"type": "integer"},
        "partner_id": {"type": "many2one", "relation": "res.partner"},
        "move_type": {"type": "selection"},
        "ref": {"type": "char"},
    },
    "account.payment": {
        "id": {"type": "integer"},
        "partner_id": {"type": "many2one", "relation": "res.partner"},
        "amount": {"type": "monetary"},
        "payment_type": {"type": "selection"},
        "partner_type": {"type": "selection"},
    },
    "crm.lead": {
        "id": {"type": "integer"},
        "name": {"type": "char"},
        "type": {"type": "selection"},
    },
    "res.partner": {"id": {"type": "integer"}, "name": {"type": "char"}},
}


class _FakeClient:
    def __init__(self, meta: dict[str, dict[str, dict[str, Any]]]) -> None:
        self.is_admin: bool | None = None
        self.admin_reason: str | None = None
        self.username = "u"
        self.uid = 1
        self.meta = meta
        self.calls: list[str] = []
        self.created: list[tuple[str, dict[str, Any]]] = []

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        self.calls.append(f"fields_get:{model}")
        return self.meta.get(model, {"id": {"type": "integer"}, "name": {"type": "char"}})

    def create(self, model: str, values: dict[str, Any]) -> int:
        self.calls.append(f"create:{model}")
        self.created.append((model, values))
        return 7


def _build(
    tmp_path: Path, meta: dict[str, dict[str, dict[str, Any]]] | None = None
) -> tuple[Dispatcher, _FakeClient]:
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
    real = OdooClient(cfg, credentials=creds)
    app_cfg = AppConfig(
        path=tmp_path / "config.toml",
        defaults=Defaults(),
        instances={cfg.name: cfg},
        audit_log_path=tmp_path / "audit.jsonl",
    )
    rl = RateLimiter()
    rl.configure(cfg.name, cfg.rate_limit_per_minute)
    fake = _FakeClient(meta if meta is not None else _META)
    rt = InstanceRuntime(config=cfg, client=real)
    rt.client = fake  # type: ignore[assignment]
    app = OdooMcpApp(
        config=app_cfg,
        audit=AuditLog(app_cfg.audit_log_path),
        prod_guard=ProdGuard(),
        rate_limiter=rl,
        instances={cfg.name: rt},
    )
    return Dispatcher(app), fake


def _call(disp: Dispatcher, args: dict[str, Any]) -> dict[str, Any]:
    contents = asyncio.run(disp.call("odoo_create", args))
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


def _create(model: str, values: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"instance": "dev", "model": model, "values": values, **extra}


def test_invoice_without_move_type_is_flagged_on_commit(tmp_path: Path) -> None:
    disp, fake = _build(tmp_path)
    payload = _call(disp, _create("account.move", {"partner_id": 3}))
    assert payload["committed"] is True
    rows = payload["menu_defaults"]
    assert [r["field"] for r in rows] == ["move_type"]
    assert "'entry'" in rows[0]["odoo_default"]
    assert "'out_invoice'" in rows[0]["web_menus_send"]
    assert "move_type" in payload["menu_defaults_note"]
    assert "before it is confirmed or posted" in payload["menu_defaults_note"]
    # The create itself is unchanged: disclosure only, nothing injected.
    assert fake.created == [("account.move", {"partner_id": 3})]


def test_invoice_without_move_type_is_flagged_on_dry_run(tmp_path: Path) -> None:
    disp, fake = _build(tmp_path)
    payload = _call(disp, _create("account.move", {"partner_id": 3}, dry_run=True))
    assert payload["preview"] is True
    assert [r["field"] for r in payload["menu_defaults"]] == ["move_type"]
    assert "dry-run again" in payload["menu_defaults_note"]
    assert fake.created == []


def test_explicit_move_type_says_nothing(tmp_path: Path) -> None:
    disp, _ = _build(tmp_path)
    payload = _call(disp, _create("account.move", {"partner_id": 3, "move_type": "out_invoice"}))
    assert "menu_defaults" not in payload
    assert "menu_defaults_note" not in payload


def test_explicit_entry_says_nothing(tmp_path: Path) -> None:
    # A deliberate journal entry is legitimate and needs no warning.
    disp, _ = _build(tmp_path)
    payload = _call(disp, _create("account.move", {"move_type": "entry", "ref": "x"}))
    assert "menu_defaults" not in payload


def test_payment_lists_only_the_missing_fields(tmp_path: Path) -> None:
    disp, _ = _build(tmp_path)
    both = _call(disp, _create("account.payment", {"partner_id": 3, "amount": 10.0}))
    assert [r["field"] for r in both["menu_defaults"]] == ["payment_type", "partner_type"]
    assert "payment_type, partner_type" in both["menu_defaults_note"]

    one = _call(
        disp,
        _create("account.payment", {"partner_id": 3, "amount": 10.0, "payment_type": "outbound"}),
    )
    assert [r["field"] for r in one["menu_defaults"]] == ["partner_type"]


def test_crm_lead_without_type_is_flagged(tmp_path: Path) -> None:
    disp, _ = _build(tmp_path)
    payload = _call(disp, _create("crm.lead", {"name": "Big deal"}))
    rows = payload["menu_defaults"]
    assert [r["field"] for r in rows] == ["type"]
    assert "'lead'" in rows[0]["odoo_default"]
    assert "Pipeline" in rows[0]["web_menus_send"]


def test_field_missing_on_instance_says_nothing(tmp_path: Path) -> None:
    # A module that does not have the field (or another version) must not be
    # told about it.
    meta = {"account.move": {"id": {"type": "integer"}, "ref": {"type": "char"}}}
    disp, _ = _build(tmp_path, meta)
    payload = _call(disp, _create("account.move", {"ref": "x"}))
    assert payload["committed"] is True
    assert "menu_defaults" not in payload


def test_other_models_say_nothing(tmp_path: Path) -> None:
    disp, _ = _build(tmp_path)
    payload = _call(disp, _create("res.partner", {"name": "Acme"}))
    assert "menu_defaults" not in payload


def test_no_extra_rpc(tmp_path: Path) -> None:
    disp, fake = _build(tmp_path)
    _call(disp, _create("account.move", {"partner_id": 3}))
    assert [c for c in fake.calls if not c.startswith("fields_get")] == ["create:account.move"]


@pytest.mark.parametrize("model", sorted(_MENU_CONTEXT_DEFAULTS))
def test_every_row_is_complete(model: str) -> None:
    for field, default, menus in _MENU_CONTEXT_DEFAULTS[model]:
        assert field and default and menus


def test_help_names_the_gotcha() -> None:
    from odoo_mcp.dispatcher import _HELP_GOTCHAS

    assert any("move_type" in g and "Journal Entry" in g for g in _HELP_GOTCHAS)
