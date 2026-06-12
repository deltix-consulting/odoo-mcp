"""Tests for the smart-field selection helper."""

from __future__ import annotations

from typing import Any

from odoo_mcp.security.fields import compile_extra_patterns
from odoo_mcp.security.smart_fields import (
    DEFAULT_SMART_FIELDS_LIMIT,
    select_smart_fields,
)


def _meta(fields: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Convenience: type-string mapping -> fields_get-shaped dict."""
    return {n: {"type": t, "string": n} for n, t in fields.items()}


def test_smart_fields_includes_id_and_name() -> None:
    out = select_smart_fields(
        "res.partner",
        _meta({"id": "integer", "name": "char", "email": "char"}),
    )
    assert "id" in out
    assert "name" in out
    assert "email" in out


def test_smart_fields_skip_binary_html_one2many_many2many() -> None:
    out = select_smart_fields(
        "x.model",
        _meta(
            {
                "id": "integer",
                "name": "char",
                "image_1920": "binary",
                "description_html": "html",
                "child_ids": "one2many",
                "tag_ids": "many2many",
            }
        ),
    )
    assert "image_1920" not in out
    assert "description_html" not in out
    assert "child_ids" not in out
    assert "tag_ids" not in out


def test_smart_fields_skip_audit_fields() -> None:
    out = select_smart_fields(
        "x.model",
        _meta(
            {
                "id": "integer",
                "name": "char",
                "create_uid": "many2one",
                "create_date": "datetime",
                "write_uid": "many2one",
                "write_date": "datetime",
                "__last_update": "datetime",
                "message_ids": "one2many",
                "activity_ids": "one2many",
            }
        ),
    )
    for skipped in (
        "create_uid",
        "create_date",
        "write_uid",
        "write_date",
        "__last_update",
        "message_ids",
        "activity_ids",
    ):
        assert skipped not in out, f"{skipped} should be skipped"


def test_smart_fields_skip_default_hidden_sensitive() -> None:
    # res.partner.vat is on _DEFAULT_HIDDEN — must be skipped.
    out = select_smart_fields(
        "res.partner",
        _meta({"id": "integer", "name": "char", "vat": "char", "email": "char"}),
    )
    assert "vat" not in out
    assert "email" in out


def test_smart_fields_skip_always_redacted_patterns() -> None:
    # 'password' / 'api_key' / '*_token' must never appear.
    out = select_smart_fields(
        "x.user",
        _meta(
            {
                "id": "integer",
                "name": "char",
                "password": "char",
                "api_key": "char",
                "auth_token": "char",
            }
        ),
    )
    assert "password" not in out
    assert "api_key" not in out
    assert "auth_token" not in out


def test_smart_fields_priority_order_first() -> None:
    # Priority fields go before the alpha-sorted fill pass.
    out = select_smart_fields(
        "x.model",
        _meta(
            {
                "id": "integer",
                "zzz_extra": "char",
                "aaa_extra": "char",
                "name": "char",
                "state": "selection",
            }
        ),
    )
    # id, name, state appear before aaa_extra and zzz_extra.
    assert out.index("id") < out.index("aaa_extra")
    assert out.index("name") < out.index("aaa_extra")
    assert out.index("state") < out.index("aaa_extra")


def test_smart_fields_caps_at_limit() -> None:
    # 50 char fields, cap is DEFAULT_SMART_FIELDS_LIMIT=25.
    fields = {f"field_{i:02d}": "char" for i in range(50)}
    fields["id"] = "integer"
    out = select_smart_fields("x.model", _meta(fields))
    assert len(out) <= DEFAULT_SMART_FIELDS_LIMIT
    assert "id" in out


def test_smart_fields_skip_noisy_name_pattern() -> None:
    out = select_smart_fields(
        "x.model",
        _meta(
            {
                "id": "integer",
                "name": "char",
                "is_company": "boolean",
                "has_unreconciled_entries": "boolean",
                "kanban_state": "selection",
                "color": "integer",
                "country_id": "many2one",
            }
        ),
    )
    assert "is_company" not in out
    assert "has_unreconciled_entries" not in out
    assert "kanban_state" not in out
    assert "color" not in out
    assert "country_id" in out


def test_smart_fields_skip_non_stored_computed_fields() -> None:
    # store=False marks a non-stored computed field — heavy, skip by default.
    fields_meta: dict[str, dict[str, Any]] = {
        "id": {"type": "integer", "store": True},
        "name": {"type": "char", "store": True},
        "computed_total": {"type": "float", "store": False},
        "stored_total": {"type": "float", "store": True},
    }
    out = select_smart_fields("x.model", fields_meta)
    assert "computed_total" not in out
    assert "stored_total" in out


def test_smart_fields_keep_field_when_store_attribute_missing() -> None:
    # Older L2 cache entries may not have the store attribute. Conservative
    # behaviour: keep the field — matches pre-v0.14.1 behaviour.
    fields_meta: dict[str, dict[str, Any]] = {
        "id": {"type": "integer"},
        "legacy_field": {"type": "char"},  # no 'store' key
    }
    out = select_smart_fields("x.model", fields_meta)
    assert "legacy_field" in out


def test_smart_fields_always_returns_at_least_id() -> None:
    # Pathological model with only audit / sensitive fields. Still gives id.
    out = select_smart_fields(
        "res.partner",
        _meta({"id": "integer", "vat": "char", "create_uid": "many2one"}),
    )
    assert out == ["id"]


# -- Model-specific priority extras (routing / logistics) ----------------------


def test_extras_surface_routing_fields_on_warehouse() -> None:
    """delivery_steps decides 1-step vs multi-step delivery — the single
    most common answer to "why did this SO confirm into the wrong picking
    type". It must be visible in a default read."""
    out = select_smart_fields(
        "stock.warehouse",
        _meta(
            {
                "id": "integer",
                "name": "char",
                "delivery_steps": "selection",
                "reception_steps": "selection",
                "delivery_route_id": "many2one",
                "partner_id": "many2one",
            }
        ),
    )
    assert "delivery_steps" in out
    assert "delivery_route_id" in out


def test_extras_bypass_heavy_type_skip_for_route_ids() -> None:
    # route_ids is many2many — normally dropped as heavy. The extras pass
    # lets it through because the ID list IS the signal.
    out = select_smart_fields(
        "product.template",
        _meta({"id": "integer", "name": "char", "route_ids": "many2many"}),
    )
    assert "route_ids" in out


def test_extras_do_not_leak_to_other_models() -> None:
    # A m2m named route_ids on an unrelated model stays heavy-skipped.
    out = select_smart_fields(
        "x.custom.model",
        _meta({"id": "integer", "name": "char", "route_ids": "many2many"}),
    )
    assert "route_ids" not in out


def test_extras_never_bypass_sensitive_policy() -> None:
    # Hypothetical: if an extras field ever collided with a sensitive
    # pattern, redaction must win over the extras pass.
    meta = _meta({"id": "integer", "name": "char", "route_id": "many2one"})
    out = select_smart_fields(
        "sale.order.line",
        meta,
        extra_redacted=compile_extra_patterns(["route_id"]),
    )
    assert "route_id" not in out


def test_extras_present_on_stock_rule() -> None:
    out = select_smart_fields(
        "stock.rule",
        _meta(
            {
                "id": "integer",
                "name": "char",
                "action": "selection",
                "picking_type_id": "many2one",
                "route_id": "many2one",
                "location_src_id": "many2one",
                "location_dest_id": "many2one",
                "procure_method": "selection",
                "sequence": "integer",
            }
        ),
    )
    for f in ("action", "picking_type_id", "route_id", "procure_method", "sequence"):
        assert f in out, f"{f} missing from stock.rule smart defaults"
