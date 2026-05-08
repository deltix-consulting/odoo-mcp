"""End-to-end tests: smart-field default through the dispatcher.

These exercise the path where the caller omits ``fields`` on
``odoo_search_read`` / ``odoo_read``. The dispatcher must compute a
curated default, pass it to the client, redact the result, and surface
``smart_fields_used`` so the caller can see what was selected.
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
    def __init__(
        self,
        fields_meta: dict[str, dict[str, Any]],
        records: list[dict[str, Any]],
    ) -> None:
        self._fields_meta = fields_meta
        self._records = records
        self.is_admin: bool | None = None
        self.admin_reason: str | None = None
        self.last_fields_arg: list[str] | None = None

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        return self._fields_meta

    def search_read(
        self,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int,
        offset: int,
        order: str | None,
    ) -> list[dict[str, Any]]:
        self.last_fields_arg = list(fields)
        # Filter to requested fields, mimicking Odoo behavior.
        return [{k: v for k, v in r.items() if k in set(fields) | {"id"}} for r in self._records]

    def read(self, model: str, ids: list[int], fields: list[str]) -> list[dict[str, Any]]:
        self.last_fields_arg = list(fields)
        return [
            {k: v for k, v in r.items() if k in set(fields) | {"id"}}
            for r in self._records
            if r["id"] in ids
        ]


def _instance_config() -> InstanceConfig:
    return InstanceConfig(
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
    rl = RateLimiter()
    rl.configure(cfg.name, cfg.rate_limit_per_minute)
    rt = InstanceRuntime(config=cfg, client=real)
    rt.client = fake  # type: ignore[assignment]
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


def _partner_meta() -> dict[str, dict[str, Any]]:
    return {
        "id": {"type": "integer", "string": "ID"},
        "name": {"type": "char", "string": "Name"},
        "email": {"type": "char", "string": "Email"},
        "vat": {"type": "char", "string": "VAT"},  # default-hidden on res.partner
        "image_1920": {"type": "binary", "string": "Image"},
        "comment": {"type": "html", "string": "Notes"},
        "child_ids": {"type": "one2many", "string": "Children"},
        "create_uid": {"type": "many2one", "string": "Created by"},
        "create_date": {"type": "datetime", "string": "Created on"},
    }


def test_search_read_smart_default_omits_fields(tmp_path: Path) -> None:
    fake = _FakeClient(
        _partner_meta(),
        [
            {
                "id": 1,
                "name": "Acme",
                "email": "a@a.com",
                "vat": "BE0123456789",
                "image_1920": "AAAAA",
                "comment": "<p>html</p>",
                "create_uid": [1, "Admin"],
            }
        ],
    )
    app = _build(tmp_path, fake)
    payload = _call(
        Dispatcher(app),
        "odoo_search_read",
        {"instance": "dev", "model": "res.partner"},
    )
    assert payload["ok"] is True
    used = payload["smart_fields_used"]
    # id and name come from priority list; email is a safe scalar.
    assert "id" in used
    assert "name" in used
    assert "email" in used
    # Sensitive (vat) and heavy (image_1920, comment, child_ids) excluded.
    assert "vat" not in used
    assert "image_1920" not in used
    assert "comment" not in used
    assert "child_ids" not in used
    # Audit fields excluded.
    assert "create_uid" not in used
    assert "create_date" not in used


def test_search_read_explicit_fields_disables_smart_path(tmp_path: Path) -> None:
    fake = _FakeClient(_partner_meta(), [{"id": 1, "name": "Acme"}])
    app = _build(tmp_path, fake)
    payload = _call(
        Dispatcher(app),
        "odoo_search_read",
        {"instance": "dev", "model": "res.partner", "fields": ["id", "name"]},
    )
    assert payload["ok"] is True
    assert "smart_fields_used" not in payload
    assert fake.last_fields_arg == ["id", "name"]


def test_search_read_has_more_when_page_full(tmp_path: Path) -> None:
    # 3 records returned with limit=2 ⇒ Odoo would have stopped at limit;
    # we mimic that by setting records to exactly the page size.
    records = [{"id": i, "name": f"r{i}"} for i in range(2)]
    fake = _FakeClient(_partner_meta(), records)
    app = _build(tmp_path, fake)
    payload = _call(
        Dispatcher(app),
        "odoo_search_read",
        {"instance": "dev", "model": "res.partner", "limit": 2, "fields": ["id", "name"]},
    )
    assert payload["ok"] is True
    assert payload["has_more"] is True
    assert payload["next_offset"] == 2


def test_search_read_next_offset_anchors_on_actual_count(tmp_path: Path) -> None:
    """next_offset must use len(records), not the requested limit.

    Defensive: if Odoo returns more rows than the limit (third-party
    module misbehaviour), anchoring on ``offset + limit`` would skip
    records on the next page. Anchoring on the actual count is correct
    in both the normal and the over-delivery case.
    """
    # 3 records returned with limit=2 — Odoo "over-delivered".
    records = [{"id": i, "name": f"r{i}"} for i in range(3)]
    fake = _FakeClient(_partner_meta(), records)
    app = _build(tmp_path, fake)
    payload = _call(
        Dispatcher(app),
        "odoo_search_read",
        {
            "instance": "dev",
            "model": "res.partner",
            "limit": 2,
            "offset": 0,
            "fields": ["id", "name"],
        },
    )
    assert payload["has_more"] is True
    # Must be 3 (len(records)), not 2 (the requested limit). With the
    # buggy version this would have been 2 and the next page would skip
    # the third record.
    assert payload["next_offset"] == 3


def test_search_read_has_more_false_when_partial_page(tmp_path: Path) -> None:
    records = [{"id": 1, "name": "only"}]
    fake = _FakeClient(_partner_meta(), records)
    app = _build(tmp_path, fake)
    payload = _call(
        Dispatcher(app),
        "odoo_search_read",
        {"instance": "dev", "model": "res.partner", "limit": 50, "fields": ["id", "name"]},
    )
    assert payload["has_more"] is False
    assert "next_offset" not in payload


def test_read_smart_default_works(tmp_path: Path) -> None:
    fake = _FakeClient(
        _partner_meta(),
        [{"id": 1, "name": "Acme", "email": "a@a.com", "vat": "BE0123"}],
    )
    app = _build(tmp_path, fake)
    payload = _call(
        Dispatcher(app),
        "odoo_read",
        {"instance": "dev", "model": "res.partner", "ids": [1]},
    )
    assert payload["ok"] is True
    used = payload["smart_fields_used"]
    assert "id" in used
    assert "name" in used
    assert "vat" not in used
