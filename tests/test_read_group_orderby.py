"""odoo_read_group: orderby must name what a group row carries.

A grouped result can be sorted by a groupby dimension, an aggregated field
or the group count. Odoo 16 silently drops any other term (log warning
only), so ``limit`` cut an unsorted list. Odoo 17+ accepts ``__count`` as a
sort key, which deltix used to refuse as an unknown field.
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

_FIELDS: dict[str, dict[str, Any]] = {
    "id": {"type": "integer"},
    "partner_id": {"type": "many2one", "relation": "res.partner"},
    "amount_total_signed": {"type": "monetary"},
    "amount_untaxed": {"type": "monetary"},
}


class _FakeClient:
    def __init__(self) -> None:
        self.orderby_sent: list[str | None] = []

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        return _FIELDS

    def read_group(
        self,
        model: str,
        domain: list[Any],
        fields: list[str],
        groupby: list[str],
        *,
        limit: int,
        offset: int,
        orderby: str | None,
        lazy: bool,
    ) -> list[dict[str, Any]]:
        self.orderby_sent.append(orderby)
        return [{"partner_id": [7, "Azure"], "partner_id_count": 3, "amount_total_signed": 9.0}]


def _dispatcher(tmp_path: Path, fake: _FakeClient) -> Dispatcher:
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
    app_cfg = AppConfig(
        path=tmp_path / "config.toml",
        defaults=Defaults(),
        instances={cfg.name: cfg},
        audit_log_path=tmp_path / "audit.jsonl",
    )
    rate_limiter = RateLimiter()
    rate_limiter.configure(cfg.name, cfg.rate_limit_per_minute)
    rt = InstanceRuntime(config=cfg, client=OdooClient(cfg, credentials=creds))
    rt.client = fake  # type: ignore[assignment]
    app = OdooMcpApp(
        config=app_cfg,
        audit=AuditLog(app_cfg.audit_log_path),
        prod_guard=ProdGuard(),
        rate_limiter=rate_limiter,
        instances={cfg.name: rt},
    )
    return Dispatcher(app)


def _top_customers(tmp_path: Path, orderby: str) -> tuple[dict[str, Any], _FakeClient]:
    fake = _FakeClient()
    contents = asyncio.run(
        _dispatcher(tmp_path, fake).call(
            "odoo_read_group",
            {
                "instance": "dev",
                "model": "account.move",
                "fields": ["amount_total_signed:sum"],
                "groupby": ["partner_id"],
                "orderby": orderby,
                "limit": 10,
            },
        )
    )
    return json.loads(contents[0].text), fake


def test_orderby_on_the_aggregate_reaches_odoo(tmp_path: Path) -> None:
    payload, fake = _top_customers(tmp_path, "amount_total_signed desc")
    assert payload["ok"] is True
    assert fake.orderby_sent == ["amount_total_signed desc"]


def test_orderby_on_the_count_reaches_odoo(tmp_path: Path) -> None:
    payload, fake = _top_customers(tmp_path, "__count desc")
    assert payload["ok"] is True
    assert fake.orderby_sent == ["__count desc"]


def test_orderby_on_the_lazy_count_name_is_sent_as_count(tmp_path: Path) -> None:
    payload, fake = _top_customers(tmp_path, "partner_id_count desc")
    assert payload["ok"] is True
    assert fake.orderby_sent == ["__count desc"]


def test_orderby_on_an_unaggregated_field_is_refused_before_odoo(tmp_path: Path) -> None:
    payload, fake = _top_customers(tmp_path, "amount_untaxed desc")
    assert payload["ok"] is False
    assert "neither grouped nor aggregated" in payload["error"]
    assert fake.orderby_sent == []
