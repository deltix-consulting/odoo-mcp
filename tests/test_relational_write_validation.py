"""Tests for x2many/many2one command validation on write paths.

Odoo interprets a relational field's write value as *command tuples* that
create, update, delete, or link records on the RELATED model. Because
``validate_write_values`` only checks top-level key names, an unpoliced
write could smuggle a mutation into a model the caller can't name directly:

* ``message_ids=[(0, 0, {...})]``  → forge a chatter message (mail.message
  is write-blocklisted).
* ``user_ids=[(1, uid, {'password': ...})]`` → reset a login password
  (res.users is write-blocklisted; password is always-redacted).
* a relation into a denylisted model (ir.actions.server).

``Dispatcher._validate_relational_writes`` runs every command's target model
through the same allowlist/denylist + write-blocklist gates the top-level
model passed, and recurses nested value dicts through the field policy.
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

_META: dict[str, dict[str, dict[str, Any]]] = {
    "res.partner": {
        "id": {"type": "integer"},
        "name": {"type": "char"},
        "user_ids": {"type": "one2many", "relation": "res.users"},
        "category_id": {"type": "many2many", "relation": "res.partner.category"},
        "parent_id": {"type": "many2one", "relation": "res.partner"},
    },
    "sale.order": {
        "id": {"type": "integer"},
        "partner_id": {"type": "many2one", "relation": "res.partner"},
        "order_line": {"type": "one2many", "relation": "sale.order.line"},
        "tag_ids": {"type": "many2many", "relation": "crm.tag"},
    },
    "sale.order.line": {
        "id": {"type": "integer"},
        "name": {"type": "char"},
        "product_uom_qty": {"type": "float"},
        "product_id": {"type": "many2one", "relation": "product.product"},
    },
    "crm.lead": {
        "id": {"type": "integer"},
        "name": {"type": "char"},
        "message_ids": {"type": "one2many", "relation": "mail.message"},
        "server_action_ids": {"type": "one2many", "relation": "ir.actions.server"},
    },
    "res.partner.category": {"id": {"type": "integer"}, "name": {"type": "char"}},
    "crm.tag": {"id": {"type": "integer"}, "name": {"type": "char"}},
}


class _FakeClient:
    def __init__(self) -> None:
        self.is_admin: bool | None = None
        self.admin_reason: str | None = None
        self.username = "u"
        self.uid = 1
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.written: list[tuple[str, list[int], dict[str, Any]]] = []

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        return _META.get(model, {"id": {"type": "integer"}, "name": {"type": "char"}})

    def create(self, model: str, values: dict[str, Any]) -> int:
        self.created.append((model, values))
        return 42

    def write(self, model: str, ids: list[int], values: dict[str, Any]) -> bool:
        self.written.append((model, ids, values))
        return True


def _build(tmp_path: Path) -> tuple[Dispatcher, _FakeClient]:
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
    fake = _FakeClient()
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


def _call(disp: Dispatcher, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    contents = asyncio.run(disp.call(tool, args))
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


# --- attack payloads are refused -------------------------------------------


def test_forge_message_via_message_ids_refused(tmp_path: Path) -> None:
    disp, fake = _build(tmp_path)
    payload = _call(
        disp,
        "odoo_create",
        {
            "instance": "dev",
            "model": "crm.lead",
            "values": {
                "name": "x",
                "message_ids": [(0, 0, {"body": "spoof", "author_id": 1})],
            },
            "dry_run": False,
        },
    )
    assert payload["ok"] is False
    assert payload["error_code"] == "model_not_allowed"
    assert fake.created == []  # never reached Odoo


def test_password_reset_via_user_ids_refused(tmp_path: Path) -> None:
    disp, fake = _build(tmp_path)
    payload = _call(
        disp,
        "odoo_write",
        {
            "instance": "dev",
            "model": "res.partner",
            "ids": [1],
            "values": {"user_ids": [(1, 7, {"password": "hunter2"})]},
            "dry_run": False,
        },
    )
    assert payload["ok"] is False
    # res.users is write-blocklisted → rejected before nested vals even matter.
    assert payload["error_code"] == "model_not_allowed"
    assert fake.written == []


def test_relation_into_denylisted_model_refused(tmp_path: Path) -> None:
    disp, fake = _build(tmp_path)
    payload = _call(
        disp,
        "odoo_create",
        {
            "instance": "dev",
            "model": "crm.lead",
            "values": {"name": "x", "server_action_ids": [(0, 0, {"name": "evil"})]},
            "dry_run": False,
        },
    )
    assert payload["ok"] is False
    assert payload["error_code"] == "model_not_allowed"
    assert fake.created == []


def test_nested_always_redacted_field_refused(tmp_path: Path) -> None:
    # Relation is allowed (sale.order.line) but the nested vals target an
    # always-redacted field — must be caught by the recursion. We fake a
    # password field onto sale.order.line for this case.
    disp, fake = _build(tmp_path)
    _META["sale.order.line"]["password"] = {"type": "char"}
    try:
        payload = _call(
            disp,
            "odoo_create",
            {
                "instance": "dev",
                "model": "sale.order",
                "values": {"order_line": [(0, 0, {"name": "l", "password": "x"})]},
                "dry_run": False,
            },
        )
    finally:
        del _META["sale.order.line"]["password"]
    assert payload["ok"] is False
    assert payload["error_code"] == "field_policy"
    assert fake.created == []


def test_many2one_given_nested_payload_refused(tmp_path: Path) -> None:
    disp, fake = _build(tmp_path)
    payload = _call(
        disp,
        "odoo_create",
        {
            "instance": "dev",
            "model": "sale.order",
            "values": {"partner_id": [(0, 0, {"name": "smuggled"})]},
            "dry_run": False,
        },
    )
    assert payload["ok"] is False
    assert payload["error_code"] == "field_policy"
    assert fake.created == []


# --- legitimate relational writes still work -------------------------------


def test_legit_nested_order_line_create_allowed(tmp_path: Path) -> None:
    disp, fake = _build(tmp_path)
    payload = _call(
        disp,
        "odoo_create",
        {
            "instance": "dev",
            "model": "sale.order",
            "values": {
                "partner_id": 5,
                "order_line": [(0, 0, {"name": "Widget", "product_uom_qty": 2})],
            },
            "dry_run": False,
        },
    )
    assert payload["ok"] is True
    assert payload["committed"] is True
    assert len(fake.created) == 1


def test_legit_m2m_link_command_allowed(tmp_path: Path) -> None:
    disp, fake = _build(tmp_path)
    payload = _call(
        disp,
        "odoo_write",
        {
            "instance": "dev",
            "model": "sale.order",
            "ids": [1],
            "values": {"tag_ids": [(6, 0, [1, 2, 3])]},
            "dry_run": False,
        },
    )
    assert payload["ok"] is True
    assert len(fake.written) == 1


def test_m2m_set_command_rejects_non_int_ids(tmp_path: Path) -> None:
    disp, fake = _build(tmp_path)
    payload = _call(
        disp,
        "odoo_write",
        {
            "instance": "dev",
            "model": "sale.order",
            "ids": [1],
            "values": {"tag_ids": [(6, 0, ["'; DROP", 2])]},
            "dry_run": False,
        },
    )
    assert payload["ok"] is False
    assert payload["error_code"] == "field_policy"
    assert fake.written == []
