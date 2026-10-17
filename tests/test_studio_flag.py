"""Test the _custom / _studio markers on odoo_describe_model output.

The dispatcher tags any field whose name starts with ``x_`` as a custom
field, and any field whose name starts with ``x_studio_`` as both custom
and Studio-origin. This is a cheap heuristic that lets Claude tell at a
glance which fields on a model are not part of standard Odoo.
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


class _FakeClient:
    def __init__(self, fields: dict[str, dict[str, Any]]) -> None:
        self._fields = fields
        self.is_admin: bool | None = None
        self.admin_reason: str | None = None
        self.username = "u"

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        return self._fields


def _build(tmp_path: Path, fields: dict[str, dict[str, Any]]) -> OdooMcpApp:
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
    rt = InstanceRuntime(config=cfg, client=real)
    rt.client = _FakeClient(fields)  # type: ignore[assignment]
    return OdooMcpApp(
        config=app_cfg,
        audit=AuditLog(app_cfg.audit_log_path),
        prod_guard=ProdGuard(),
        rate_limiter=rl,
        instances={cfg.name: rt},
    )


def _call(disp: Dispatcher, args: dict[str, Any]) -> dict[str, Any]:
    contents = asyncio.run(disp.call("odoo_describe_model", args))
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


def test_studio_field_marked_custom_and_studio(tmp_path: Path) -> None:
    fields = {
        "id": {"type": "integer", "string": "ID"},
        "name": {"type": "char", "string": "Name"},
        "x_studio_priority": {"type": "selection", "string": "Priority"},
    }
    app = _build(tmp_path, fields)
    payload = _call(Dispatcher(app), {"instance": "dev", "model": "res.partner"})
    assert payload["ok"] is True
    studio = payload["fields"]["x_studio_priority"]
    assert studio.get("_custom") is True
    assert studio.get("_studio") is True
    # Standard fields don't get the markers.
    assert "_custom" not in payload["fields"]["name"]
    assert "_studio" not in payload["fields"]["name"]


def test_x_prefixed_field_marked_custom_only(tmp_path: Path) -> None:
    fields = {
        "id": {"type": "integer", "string": "ID"},
        "x_legacy_field": {"type": "char", "string": "Legacy"},
    }
    app = _build(tmp_path, fields)
    payload = _call(Dispatcher(app), {"instance": "dev", "model": "res.partner"})
    legacy = payload["fields"]["x_legacy_field"]
    assert legacy.get("_custom") is True
    assert "_studio" not in legacy


def test_no_custom_fields_no_markers(tmp_path: Path) -> None:
    fields = {
        "id": {"type": "integer", "string": "ID"},
        "name": {"type": "char", "string": "Name"},
        "email": {"type": "char", "string": "Email"},
    }
    app = _build(tmp_path, fields)
    payload = _call(Dispatcher(app), {"instance": "dev", "model": "res.partner"})
    for fname in ("id", "name", "email"):
        meta = payload["fields"][fname]
        assert "_custom" not in meta
        assert "_studio" not in meta


# --- translate / company_dependent ------------------------------------------
# Odoo stores these per language / per company; one call touches one value.

_SCOPED_FIELDS: dict[str, dict[str, Any]] = {
    "name": {"type": "char", "string": "Name", "translate": True},
    "standard_price": {"type": "float", "string": "Cost", "company_dependent": True},
    "default_code": {"type": "char", "string": "Reference", "translate": False},
}


def test_translate_and_company_dependent_marked_by_default(tmp_path: Path) -> None:
    disp = Dispatcher(_build(tmp_path, _SCOPED_FIELDS))
    out = _call(disp, {"instance": "dev", "model": "product.template"})
    assert out["fields"]["name"]["translate"] is True
    assert out["fields"]["standard_price"]["company_dependent"] is True
    # Falsy flags are dropped in the minimal response.
    assert "translate" not in out["fields"]["default_code"]
    notes = out["notes"]
    assert len(notes) == 2
    assert "en_US" in notes[0] and "only the en_US value" in notes[0]
    assert "current company only" in notes[1]


def test_translate_and_company_dependent_kept_in_verbose(tmp_path: Path) -> None:
    disp = Dispatcher(_build(tmp_path, _SCOPED_FIELDS))
    out = _call(disp, {"instance": "dev", "model": "product.template", "verbose": True})
    assert out["fields"]["name"]["translate"] is True
    assert out["fields"]["standard_price"]["company_dependent"] is True
    assert len(out["notes"]) == 2


def test_no_scoped_fields_no_notes(tmp_path: Path) -> None:
    fields = {"name": {"type": "char", "string": "Name", "translate": False}}
    disp = Dispatcher(_build(tmp_path, fields))
    out = _call(disp, {"instance": "dev", "model": "res.partner"})
    assert "notes" not in out
    assert out["fields"]["name"] == {"type": "char", "string": "Name"}
