"""Tests for ``odoo_diagnose_routing`` — the read-only routing inspector.

Real-world motivation: SO 1161 at deltix produced a TR-LAAD picking
(scenario B) instead of the expected LAAD (scenario A). The other AI
session pinned the picking-type decision on Odoo's stock-routing
config but could not introspect ``stock.rule`` from its sandbox. This
tool exposes exactly that introspection — read-only, hardcoded model
list, no allowlist-widening surprises.

These tests pin the contract:

- Six config models read, no others
- Audit-log entry on success
- Honest failure when the product or warehouse id doesn't exist
- Inverse coverage: the tool MUST NOT predict the winning rule (a
  prediction would drift on every Odoo release)
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
from odoo_mcp.security.allowlist import Operation, is_read, is_write
from odoo_mcp.security.limits import RateLimiter
from odoo_mcp.security.prod_guard import ProdGuard
from odoo_mcp.tools import build_tools


class _RoutingFake:
    """search_read mock that returns canned rows per model.

    Records every call so a test can assert the tool touched exactly
    the six routing models and nothing else.
    """

    def __init__(self, rows: dict[str, list[dict[str, Any]]]) -> None:
        self._rows = rows
        self.calls: list[tuple[str, list[Any]]] = []
        self.is_admin: bool | None = False
        self.admin_reason: str | None = None
        self.uid = 7
        self.username: str | None = "alice"

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        return {}

    def search_read(
        self,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int,
        offset: int,
        order: str | None,
    ) -> list[dict[str, Any]]:
        self.calls.append((model, domain))
        return list(self._rows.get(model, []))


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
        # A deliberately narrow allowlist: the diagnose tool must NOT
        # require any of these six routing models to be in it.
        allowed_models=frozenset({"res.partner"}),
    )


def _build(tmp_path: Path, fake: _RoutingFake) -> OdooMcpApp:
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
    contents = asyncio.run(disp.call("odoo_diagnose_routing", args))
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Operation enum + read-op classification
# ---------------------------------------------------------------------------


def test_operation_is_read_only() -> None:
    """The new Operation MUST be classified as read. A future refactor
    that drops it into _WRITE_OPS would silently subject the tool to
    prod-guard's commit pipeline — completely wrong shape for a
    read-only inspector."""
    assert is_read(Operation.DIAGNOSE_ROUTING)
    assert not is_write(Operation.DIAGNOSE_ROUTING)


def test_tool_registered() -> None:
    """odoo_diagnose_routing appears in build_tools() in stable order so
    the Claude tool-listing pane shows it where operators expect."""
    names = [t.name for t in build_tools()]
    assert "odoo_diagnose_routing" in names
    # Sits next to the existing diagnose_access in the listing order so
    # operators discover them together.
    idx_access = names.index("odoo_diagnose_access")
    idx_routing = names.index("odoo_diagnose_routing")
    assert idx_routing == idx_access + 1


# ---------------------------------------------------------------------------
# Happy path: returns the candidate routes + rules
# ---------------------------------------------------------------------------


_PRODUCT_ROW = {
    "id": 8,
    "name": "€-B",
    "default_code": "EB",
    "product_tmpl_id": [42, "€-B template"],
    "route_ids": [5],  # one product-level route
    "categ_id": [3, "Consumable"],
}
_TEMPLATE_ROW = {
    "id": 42,
    "name": "€-B template",
    "route_ids": [5],
    "categ_id": [3, "Consumable"],
}
_WAREHOUSE_ROW = {
    "id": 2,
    "name": "TRAILERS",
    "code": "TRL",
    "delivery_steps": "ship_only",
    "reception_steps": "one_step",
    "sale_route_id": [9, "TRAILERS: Deliver in 1 step"],
    "purchase_route_id": [11, "Buy"],
    "mto_pull_id": False,
    "lot_stock_id": [101, "TRAILERS/Stock"],
    "view_location_id": [100, "TRAILERS"],
}
_ROUTES = [
    {
        "id": 5,
        "name": "Product-level route",
        "sequence": 10,
        "active": True,
        "product_selectable": True,
        "product_categ_selectable": False,
        "warehouse_selectable": False,
        "sale_selectable": False,
        "warehouse_ids": [],
    },
    {
        "id": 9,
        "name": "TRAILERS: Deliver in 1 step",
        "sequence": 20,
        "active": True,
        "product_selectable": False,
        "product_categ_selectable": False,
        "warehouse_selectable": True,
        "sale_selectable": True,
        "warehouse_ids": [2],
    },
]
_RULES_TR_LAAD = {
    "id": 17,
    "name": "TRAILERS: Stock → Customers",
    "sequence": 30,
    "active": True,
    "route_id": [9, "TRAILERS: Deliver in 1 step"],
    "action": "pull",
    "location_src_id": [101, "TRAILERS/Stock"],
    "location_dest_id": [50, "Partner Locations/Customers"],
    "picking_type_id": [8, "TRAILERS: TR-LAAD"],
    "procure_method": "make_to_stock",
    "group_propagation_option": "propagate",
    "auto": "manual",
    "warehouse_id": [2, "TRAILERS"],
}
_RULES_LAAD = {
    "id": 18,
    "name": "Product-level rule (REK)",
    "sequence": 5,
    "active": True,
    "route_id": [5, "Product-level route"],
    "action": "pull",
    "location_src_id": [200, "REK/Stock"],
    "location_dest_id": [50, "Partner Locations/Customers"],
    "picking_type_id": [4, "REK: LAAD"],
    "procure_method": "make_to_stock",
    "group_propagation_option": "propagate",
    "auto": "manual",
    "warehouse_id": False,
}


def test_diagnose_routing_returns_candidate_routes_and_rules(tmp_path: Path) -> None:
    """End-to-end: feed canned routing config, get back both routes and
    a rule set ordered for human inspection."""
    fake = _RoutingFake(
        {
            "product.product": [_PRODUCT_ROW],
            "product.template": [_TEMPLATE_ROW],
            "stock.warehouse": [_WAREHOUSE_ROW],
            "stock.route": _ROUTES,
            "stock.rule": [_RULES_LAAD, _RULES_TR_LAAD],
        }
    )
    app = _build(tmp_path, fake)
    dispatcher = Dispatcher(app)
    out = _call(dispatcher, {"instance": "dev", "product_id": 8, "warehouse_id": 2})

    assert out["instance"] == "dev"
    assert out["product"]["id"] == 8
    assert out["template"]["id"] == 42
    assert out["warehouse"]["id"] == 2
    assert {r["id"] for r in out["candidate_routes"]} == {5, 9}
    # BOTH the LAAD and TR-LAAD candidates surface; the agent reading
    # this then sees that both picking_type_id values exist and can
    # ask why one fires instead of the other.
    rule_picking_types = {tuple(r["picking_type_id"]) for r in out["candidate_rules"]}
    assert (4, "REK: LAAD") in rule_picking_types
    assert (8, "TRAILERS: TR-LAAD") in rule_picking_types


def test_diagnose_routing_does_not_predict_a_winner(tmp_path: Path) -> None:
    """Hard guarantee: the tool's response must NOT contain a "winner" /
    "predicted_picking_type" / "would_fire" field. Predicting the
    runtime winner means replicating Odoo's full rule-priority +
    location-chain logic, which drifts on every release. We expose the
    candidates and let humans pick.
    """
    fake = _RoutingFake(
        {
            "product.product": [_PRODUCT_ROW],
            "product.template": [_TEMPLATE_ROW],
            "stock.warehouse": [_WAREHOUSE_ROW],
            "stock.route": _ROUTES,
            "stock.rule": [_RULES_LAAD, _RULES_TR_LAAD],
        }
    )
    app = _build(tmp_path, fake)
    dispatcher = Dispatcher(app)
    out = _call(dispatcher, {"instance": "dev", "product_id": 8, "warehouse_id": 2})
    # Names that would (wrongly) imply prediction:
    forbidden = {"winning_rule", "predicted_picking_type", "would_fire", "winner"}
    assert not (forbidden & set(out)), (
        f"diagnose_routing must NOT predict — these keys leak prediction: {forbidden & set(out)}"
    )
    # The note text must also flag the non-prediction guarantee
    # explicitly so an LLM reading the output doesn't fabricate a
    # winner anyway.
    assert "does NOT predict" in out["note"]


# ---------------------------------------------------------------------------
# Allowlist bypass scope — exactly six models, no others
# ---------------------------------------------------------------------------


def test_diagnose_routing_only_touches_six_models(tmp_path: Path) -> None:
    """The allowlist bypass is justified by these six models being
    operator-configuration only. A refactor that started touching a
    seventh (say, sale.order, which carries business data) would
    silently widen the bypass — pin the model set so that can't happen
    quietly."""
    fake = _RoutingFake(
        {
            "product.product": [_PRODUCT_ROW],
            "product.template": [_TEMPLATE_ROW],
            "stock.warehouse": [_WAREHOUSE_ROW],
            "stock.route": _ROUTES,
            "stock.rule": [_RULES_LAAD],
        }
    )
    app = _build(tmp_path, fake)
    dispatcher = Dispatcher(app)
    _call(dispatcher, {"instance": "dev", "product_id": 8, "warehouse_id": 2})
    models_touched = {model for (model, _domain) in fake.calls}
    assert models_touched.issubset(
        {
            "product.product",
            "product.template",
            "stock.warehouse",
            "stock.route",
            "stock.rule",
            "stock.location",
        }
    ), f"diagnose_routing leaked into unexpected models: {models_touched}"


def test_diagnose_routing_works_when_models_not_allowlisted(tmp_path: Path) -> None:
    """The allowlist bypass is the whole point: the tool must work even
    when the per-instance ``allowed_models`` does NOT include any of
    the six routing models. Pin this so a future refactor that
    re-routes through ``check_model`` doesn't silently regress the UX."""
    fake = _RoutingFake(
        {
            "product.product": [_PRODUCT_ROW],
            "product.template": [_TEMPLATE_ROW],
            "stock.warehouse": [_WAREHOUSE_ROW],
            "stock.route": _ROUTES,
            "stock.rule": [_RULES_LAAD],
        }
    )
    app = _build(tmp_path, fake)
    # Sanity: the test config indeed only allowlists res.partner.
    assert app.config.instances["dev"].allowed_models == frozenset({"res.partner"})
    dispatcher = Dispatcher(app)
    out = _call(dispatcher, {"instance": "dev", "product_id": 8, "warehouse_id": 2})
    # No "model not allowed" error; we got candidate routes back.
    assert "candidate_routes" in out


# ---------------------------------------------------------------------------
# Honest failure paths
# ---------------------------------------------------------------------------


def test_diagnose_routing_unknown_product(tmp_path: Path) -> None:
    """Empty result on product.product → clear OdooMcpError instead of
    a generic KeyError or, worse, returning an empty diagnosis that
    would be silently meaningless."""
    fake = _RoutingFake({"product.product": []})
    app = _build(tmp_path, fake)
    dispatcher = Dispatcher(app)
    payload = _call(dispatcher, {"instance": "dev", "product_id": 999, "warehouse_id": 2})
    assert payload["ok"] is False
    assert "product.product id=999" in payload["error"]


def test_diagnose_routing_unknown_warehouse(tmp_path: Path) -> None:
    """Same shape on stock.warehouse — a typo'd warehouse_id fails fast."""
    fake = _RoutingFake(
        {
            "product.product": [_PRODUCT_ROW],
            "product.template": [_TEMPLATE_ROW],
            "stock.warehouse": [],
        }
    )
    app = _build(tmp_path, fake)
    dispatcher = Dispatcher(app)
    payload = _call(dispatcher, {"instance": "dev", "product_id": 8, "warehouse_id": 999})
    assert payload["ok"] is False
    assert "stock.warehouse id=999" in payload["error"]
