"""``odoo_diagnose_routing`` must apply the same field policy as every other read tool.

The tool builds its own hardcoded ``search_read`` calls against six
operator-configuration models instead of going through the caller-driven
read path, so nothing upstream had ever applied the instance's field
policy to the rows it returns. That made it the one read handler where
an operator's ``sensitive_fields`` / ``custom_sensitive_field_patterns``
silently did not apply — ``odoo_search_read`` on ``product.product``
would hide a field that ``odoo_diagnose_routing`` handed straight back.

These tests pin both halves of the fix:

- every returned block (product, template, warehouse, routes, rules) is
  redacted, so a future block can't be added unredacted; and
- redaction is an OUTPUT policy — hiding ``route_ids`` must not change
  which routes and rules the diagnosis finds.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from test_diagnose_routing import (  # reuse the canned routing config
    _PRODUCT_ROW,
    _ROUTES,
    _RULES_LAAD,
    _RULES_TR_LAAD,
    _TEMPLATE_ROW,
    _WAREHOUSE_ROW,
    _call,
    _RoutingFake,
)

from odoo_mcp.audit import AuditLog
from odoo_mcp.client import OdooClient
from odoo_mcp.config import AppConfig, Defaults, InstanceConfig
from odoo_mcp.credentials import Credentials
from odoo_mcp.dispatcher import Dispatcher, InstanceRuntime, OdooMcpApp
from odoo_mcp.security.fields import compile_extra_patterns
from odoo_mcp.security.limits import RateLimiter
from odoo_mcp.security.prod_guard import ProdGuard

_ALL_ROWS: dict[str, list[dict[str, Any]]] = {
    "product.product": [_PRODUCT_ROW],
    "product.template": [_TEMPLATE_ROW],
    "stock.warehouse": [_WAREHOUSE_ROW],
    "stock.route": _ROUTES,
    "stock.rule": [_RULES_LAAD, _RULES_TR_LAAD],
}


def _build(
    tmp_path: Path,
    fake: _RoutingFake,
    *,
    sensitive_fields: dict[str, frozenset[str]] | None = None,
    patterns: tuple[str, ...] = (),
) -> OdooMcpApp:
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
        allowed_models=frozenset({"res.partner"}),
        sensitive_fields=sensitive_fields or {},
        custom_sensitive_field_patterns=patterns,
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
    rt = InstanceRuntime(
        config=cfg,
        client=OdooClient(cfg, credentials=creds),
        extra_redacted=compile_extra_patterns(list(patterns)),
    )
    rt.client = fake  # type: ignore[assignment]
    return OdooMcpApp(
        config=app_cfg,
        audit=AuditLog(app_cfg.audit_log_path),
        prod_guard=ProdGuard(),
        rate_limiter=rl,
        instances={cfg.name: rt},
    )


def _diagnose(app: OdooMcpApp) -> dict[str, Any]:
    return _call(Dispatcher(app), {"instance": "dev", "product_id": 8, "warehouse_id": 2})


# ---------------------------------------------------------------------------
# custom_sensitive_field_patterns
# ---------------------------------------------------------------------------


def test_custom_pattern_applies_to_all_blocks(tmp_path: Path) -> None:
    """A pattern the operator wrote to mean "never show this" must hold
    across all five blocks. Testing one block would let a future sixth
    block ship unredacted; ``name`` exists on every routing model, so
    this covers them all at once."""
    fake = _RoutingFake(_ALL_ROWS)
    out = _diagnose(_build(tmp_path, fake, patterns=("name",)))

    assert "name" not in out["product"]
    assert "name" not in out["template"]
    assert "name" not in out["warehouse"]
    assert all("name" not in r for r in out["candidate_routes"])
    assert all("name" not in r for r in out["candidate_rules"])
    # Everything else survives — this is redaction, not truncation.
    assert out["product"]["id"] == 8
    assert {r["id"] for r in out["candidate_routes"]} == {5, 9}


def test_custom_pattern_hides_internal_reference(tmp_path: Path) -> None:
    """The realistic case: a manufacturer redacts the internal SKU.
    ``odoo_search_read`` on product.product already refuses it; before
    this fix ``odoo_diagnose_routing`` returned it verbatim."""
    fake = _RoutingFake(_ALL_ROWS)
    out = _diagnose(_build(tmp_path, fake, patterns=(r"default_code",)))
    assert "default_code" not in out["product"]
    assert out["product"]["name"] == "€-B"


# ---------------------------------------------------------------------------
# per-model sensitive_fields overrides
# ---------------------------------------------------------------------------


def test_instance_sensitive_fields_override_is_honoured(tmp_path: Path) -> None:
    """``[instances.dev.sensitive_fields]`` is per-model and needs a
    per-call opt-in that this tool has no argument for — so a listed
    field is simply absent."""
    fake = _RoutingFake(_ALL_ROWS)
    out = _diagnose(
        _build(
            tmp_path,
            fake,
            sensitive_fields={"stock.warehouse": frozenset({"code", "lot_stock_id"})},
        )
    )
    assert "code" not in out["warehouse"]
    assert "lot_stock_id" not in out["warehouse"]
    assert out["warehouse"]["name"] == "TRAILERS"
    # Scoped to the model it names — product.product keeps its own fields.
    assert out["product"]["default_code"] == "EB"


def test_redaction_does_not_change_which_rules_are_found(tmp_path: Path) -> None:
    """Redaction is an output policy. Hiding ``route_ids`` on the product
    must not break the join that discovers the candidate routes/rules —
    otherwise a privacy setting would silently degrade the diagnosis
    into a wrong answer rather than a quieter one."""
    fake = _RoutingFake(_ALL_ROWS)
    out = _diagnose(
        _build(tmp_path, fake, sensitive_fields={"product.product": frozenset({"route_ids"})})
    )
    assert "route_ids" not in out["product"]
    assert {r["id"] for r in out["candidate_routes"]} == {5, 9}
    assert {r["id"] for r in out["candidate_rules"]} == {17, 18}


# ---------------------------------------------------------------------------
# No-policy instances and cost
# ---------------------------------------------------------------------------


def test_unconfigured_instance_gets_every_field_back(tmp_path: Path) -> None:
    """Regression guard for the fix itself: with no policy configured the
    response must be byte-identical to the raw Odoo rows. A redactor that
    dropped fields it had no type for would silently gut this tool."""
    fake = _RoutingFake(_ALL_ROWS)
    out = _diagnose(_build(tmp_path, fake))
    assert out["product"] == _PRODUCT_ROW
    assert out["template"] == _TEMPLATE_ROW
    assert out["warehouse"] == _WAREHOUSE_ROW
    assert out["candidate_routes"] == _ROUTES
    assert out["candidate_rules"] == [_RULES_LAAD, _RULES_TR_LAAD]


def test_no_fields_get_for_models_with_no_rows(tmp_path: Path) -> None:
    """A product with no routes never reaches stock.route / stock.rule
    today. Redaction must not change that: an unconditional ``fields_get``
    would add both an RPC and a new failure mode on instances where the
    routing models aren't reachable."""
    rows = {
        "product.product": [{**_PRODUCT_ROW, "route_ids": []}],
        "product.template": [{**_TEMPLATE_ROW, "route_ids": []}],
        "stock.warehouse": [{**_WAREHOUSE_ROW, "sale_route_id": False}],
    }
    fake = _RoutingFake(rows)
    out = _diagnose(_build(tmp_path, fake, patterns=("name",)))
    assert out["candidate_routes"] == []
    assert out["candidate_rules"] == []
    assert "stock.route" not in fake.fields_get_calls
    assert "stock.rule" not in fake.fields_get_calls


def test_binary_field_is_replaced_with_a_size_placeholder(tmp_path: Path) -> None:
    """Odoo's ``image_*`` fields live on product.template. None are in the
    hardcoded field list today, but the placeholder path is what keeps a
    future addition from dumping a base64 blob into the model context."""
    blob = "A" * 4000
    rows = {
        **_ALL_ROWS,
        "product.template": [{**_TEMPLATE_ROW, "image_1920": blob}],
    }
    meta = {
        "product.template": {
            "id": {"type": "integer"},
            "name": {"type": "char"},
            "route_ids": {"type": "many2many"},
            "categ_id": {"type": "many2one"},
            "image_1920": {"type": "binary"},
        }
    }
    fake = _RoutingFake(rows, fields_meta=meta)
    out = _diagnose(_build(tmp_path, fake))
    assert out["template"]["image_1920"].startswith("<binary")
    assert blob not in out["template"]["image_1920"]
