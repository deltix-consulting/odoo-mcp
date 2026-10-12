"""A string compared to a relational field is a search on the RELATED model.

Odoo rewrites ``('partner_id', 'ilike', 'x')`` as ``('partner_id', 'any',
[('display_name', 'ilike', 'x')])`` (16.0-18.0 via ``_name_search`` /
``_search`` on ``display_name``, 19.0+ in ``_optimize_relational_name_search``),
and ``res.partner._search_display_name`` matches every field in
``_rec_names_search`` — ``vat`` and ``company_registry`` included on 16.0-20.0,
both default-hidden in deltix. So, before this fix,

    odoo_search_count(sale.order, [('partner_id', '=like', 'BE0477%')])

recovered a customer's VAT number one prefix at a time with no opt-in, on a
model the caller never named — the dotted-path traversal the sandbox refuses,
in another spelling. Ids, ``False`` and ``None`` stay allowed.
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
from odoo_mcp.errors import DomainSandboxError
from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD
from odoo_mcp.security.domain import sandbox_domain
from odoo_mcp.security.limits import RateLimiter
from odoo_mcp.security.prod_guard import ProdGuard

_FIELDS = frozenset({"id", "name", "state", "partner_id", "tag_ids", "order_line"})
_RELATIONAL = frozenset({"partner_id", "tag_ids", "order_line"})


# --- sandbox ---------------------------------------------------------------


@pytest.mark.parametrize(
    "leaf",
    [
        ("partner_id", "=like", "BE0477%"),  # VAT prefix oracle
        ("partner_id", "ilike", "Acme"),
        ("partner_id", "=", "Acme"),
        ("partner_id", "!=", "Acme"),
        ("partner_id", "not ilike", "Acme"),
        ("partner_id", "in", ["Acme"]),
        ("partner_id", "in", [7, "Acme"]),  # Odoo splits the str part off
        ("partner_id", "child_of", "Acme"),
        ("tag_ids", "ilike", "vip"),  # many2many
        ("order_line", "ilike", "desk"),  # one2many
    ],
)
def test_string_on_relational_field_is_refused(leaf: tuple[str, str, Any]) -> None:
    with pytest.raises(DomainSandboxError, match="relational") as exc:
        sandbox_domain([leaf], _FIELDS, model="sale.order", relational_fields=_RELATIONAL)
    # The refusal names the two-call route, as the dotted-path refusal does.
    assert "odoo_lookup" in str(exc.value)
    assert f"({leaf[0]!r}, 'in', [<those ids>])" in str(exc.value)


@pytest.mark.parametrize(
    "leaf",
    [
        ("partner_id", "=", 7),
        ("partner_id", "in", [7, 8]),
        ("partner_id", "=", False),
        ("partner_id", "!=", None),
        ("partner_id", "child_of", 7),
        ("partner_id", "child_of", [7]),
        ("tag_ids", "in", []),
        ("name", "ilike", "Acme"),  # a string on a non-relational field
        ("state", "in", ["draft", "sent"]),
    ],
)
def test_ids_and_non_relational_strings_still_pass(leaf: tuple[str, str, Any]) -> None:
    out = sandbox_domain([leaf], _FIELDS, model="sale.order", relational_fields=_RELATIONAL)
    assert out == [leaf]


def test_inside_logical_operators_too() -> None:
    with pytest.raises(DomainSandboxError, match="relational"):
        sandbox_domain(
            ["|", ("state", "=", "sale"), "!", ("partner_id", "ilike", "BE04")],
            _FIELDS,
            model="sale.order",
            relational_fields=_RELATIONAL,
        )


# --- every domain-taking tool passes the relational set ---------------------

_META: dict[str, dict[str, dict[str, Any]]] = {
    "sale.order": {
        "id": {"type": "integer"},
        "name": {"type": "char"},
        "state": {"type": "selection"},
        "amount_total": {"type": "monetary"},
        "partner_id": {"type": "many2one", "relation": "res.partner"},
        "tag_ids": {"type": "many2many", "relation": "crm.tag"},
    },
}


class _FakeClient:
    def __init__(self) -> None:
        self.is_admin: bool | None = None
        self.admin_reason: str | None = None
        self.username = "u"
        self.uid = 1
        self.domains: list[list[Any]] = []

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        return _META[model]

    def search_read(self, model: str, domain: list[Any], *a: Any, **k: Any) -> list[Any]:
        self.domains.append(domain)
        return []

    def search_count(self, model: str, domain: list[Any]) -> int:
        self.domains.append(domain)
        return 0

    def read_group(self, model: str, domain: list[Any], *a: Any, **k: Any) -> list[Any]:
        self.domains.append(domain)
        return []


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
    app_cfg = AppConfig(
        path=tmp_path / "config.toml",
        defaults=Defaults(),
        instances={cfg.name: cfg},
        audit_log_path=tmp_path / "audit.jsonl",
    )
    rl = RateLimiter()
    rl.configure(cfg.name, cfg.rate_limit_per_minute)
    fake = _FakeClient()
    rt = InstanceRuntime(config=cfg, client=OdooClient(cfg, credentials=creds))
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


_TOOLS: list[tuple[str, dict[str, Any]]] = [
    ("odoo_search_read", {"fields": ["name"]}),
    ("odoo_search_count", {}),
    ("odoo_read_group", {"fields": ["amount_total:sum"], "groupby": ["state"]}),
]


@pytest.mark.parametrize(("tool", "extra"), _TOOLS)
def test_tool_refuses_vat_prefix_through_partner_id(
    tmp_path: Path, tool: str, extra: dict[str, Any]
) -> None:
    disp, fake = _build(tmp_path)
    payload = _call(
        disp,
        tool,
        {
            "instance": "dev",
            "model": "sale.order",
            "domain": [["partner_id", "=like", "BE0477%"]],
            **extra,
        },
    )
    assert payload["ok"] is False
    assert payload["error_code"] == "domain_sandbox"
    assert fake.domains == []  # nothing reached Odoo


@pytest.mark.parametrize(("tool", "extra"), _TOOLS)
def test_tool_still_filters_by_id(tmp_path: Path, tool: str, extra: dict[str, Any]) -> None:
    disp, fake = _build(tmp_path)
    payload = _call(
        disp,
        tool,
        {
            "instance": "dev",
            "model": "sale.order",
            "domain": [["partner_id", "in", [7]], ["name", "ilike", "S00"]],
            **extra,
        },
    )
    assert payload["ok"] is True, payload
    assert len(fake.domains) == 1
