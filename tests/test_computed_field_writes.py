"""Tests for the writability check on odoo_create / odoo_write values.

Odoo's ``write()`` does not validate that a field can actually receive a
value. For a field that is computed with no ``inverse``, ``fields_get``
reports ``readonly=True`` (Odoo sets it itself:
``attrs['readonly'] = attrs.get('readonly', not attrs.get('inverse'))``),
and when that field is also ``store=False`` the value goes nowhere:
``write()`` calls ``field.write(self, value)``, which only touches the
cache; ``determine_inverses`` is empty; and the SQL path asserts
``field.store and field.column_type``. ``write()` still returns ``True``.

So the agent gets ``committed: true`` for a write that did nothing. These
tests pin the two halves of the response:

* non-stored + readonly  -> refused outright (never persists, no exceptions)
* stored + readonly      -> committed, but reported back, because a stored
  computed field is recomputed from its dependencies and the value is lost
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
from odoo_mcp.errors import FieldPolicyError
from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD
from odoo_mcp.security.fields import recomputed_write_fields, validate_write_values
from odoo_mcp.security.limits import RateLimiter
from odoo_mcp.security.prod_guard import ProdGuard

# A shape mirroring what fields_get returns for sale.order: a writable
# column, a stored computed total, a non-stored computed display field,
# and a non-stored field that DOES have an inverse (readonly=False).
_SALE_ORDER_FIELDS: dict[str, dict[str, Any]] = {
    "id": {"type": "integer", "readonly": True, "store": True},
    "name": {"type": "char", "readonly": False, "store": True},
    "note": {"type": "text", "readonly": False, "store": True},
    # stored computed: reaches the column, then gets recomputed away
    "amount_total": {"type": "monetary", "readonly": True, "store": True},
    # non-stored computed, no inverse: Odoo accepts and discards
    "display_name": {"type": "char", "readonly": True, "store": False},
    "tax_country_id": {"type": "many2one", "readonly": True, "store": False},
    # non-stored but has an inverse -> readonly False -> legitimately writable
    "some_inversed": {"type": "char", "readonly": False, "store": False},
}

_KNOWN = frozenset(_SALE_ORDER_FIELDS)


class _FakeClient:
    def __init__(self, fields: dict[str, dict[str, Any]]) -> None:
        self._fields = fields
        self.write_calls: list[tuple[str, list[int], dict[str, Any]]] = []
        self.create_calls: list[tuple[str, dict[str, Any]]] = []
        self.is_admin: bool | None = None
        self.admin_reason: str | None = None

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        return self._fields

    def write(self, model: str, ids: list[int], values: dict[str, Any]) -> bool:
        self.write_calls.append((model, ids, values))
        return True

    def create(self, model: str, values: dict[str, Any]) -> int:
        self.create_calls.append((model, values))
        return 42


def _build_app(tmp_path: Path) -> tuple[OdooMcpApp, _FakeClient]:
    inst_cfg = InstanceConfig(
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
    creds = Credentials(instance_name=inst_cfg.name, username="u", _api_key="k" * 10)
    real_client = OdooClient(inst_cfg, credentials=creds)
    fake = _FakeClient(_SALE_ORDER_FIELDS)
    app_cfg = AppConfig(
        path=tmp_path / "config.toml",
        defaults=Defaults(),
        instances={inst_cfg.name: inst_cfg},
        audit_log_path=tmp_path / "audit.jsonl",
    )
    rate_limiter = RateLimiter()
    rate_limiter.configure(inst_cfg.name, inst_cfg.rate_limit_per_minute)
    rt = InstanceRuntime(config=inst_cfg, client=real_client)
    rt.client = fake  # type: ignore[assignment]
    app = OdooMcpApp(
        config=app_cfg,
        audit=AuditLog(app_cfg.audit_log_path),
        prod_guard=ProdGuard(),
        rate_limiter=rate_limiter,
        instances={inst_cfg.name: rt},
    )
    return app, fake


def _call(dispatcher: Dispatcher, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    contents = asyncio.run(dispatcher.call(tool, args))
    assert len(contents) == 1
    payload: dict[str, Any] = json.loads(contents[0].text)
    return payload


# -- validate_write_values: the refusal ---------------------------------------


@pytest.mark.parametrize("field", ["display_name", "tax_country_id"])
def test_refuses_non_stored_computed_field(field: str) -> None:
    with pytest.raises(FieldPolicyError, match="computed and not stored"):
        validate_write_values(
            "sale.order",
            {"name": "SO001", field: "x"},
            _KNOWN,
            fields_meta=_SALE_ORDER_FIELDS,
        )


def test_refusal_names_the_field_and_the_remedy() -> None:
    with pytest.raises(FieldPolicyError) as excinfo:
        validate_write_values(
            "sale.order", {"display_name": "x"}, _KNOWN, fields_meta=_SALE_ORDER_FIELDS
        )
    msg = str(excinfo.value)
    assert "display_name" in msg
    assert "sale.order" in msg
    # The caller needs to know Odoo would have reported success.
    assert "persists nothing" in msg


def test_stored_readonly_field_is_not_refused() -> None:
    out = validate_write_values(
        "sale.order", {"amount_total": 100.0}, _KNOWN, fields_meta=_SALE_ORDER_FIELDS
    )
    assert out == {"amount_total": 100.0}


def test_non_stored_field_with_inverse_is_writable() -> None:
    # readonly=False means Odoo has an inverse to route the write through.
    out = validate_write_values(
        "sale.order", {"some_inversed": "x"}, _KNOWN, fields_meta=_SALE_ORDER_FIELDS
    )
    assert out == {"some_inversed": "x"}


def test_writable_column_unaffected() -> None:
    out = validate_write_values(
        "sale.order", {"name": "SO001", "note": "hi"}, _KNOWN, fields_meta=_SALE_ORDER_FIELDS
    )
    assert out == {"name": "SO001", "note": "hi"}


# -- fail-open guards: never manufacture a false refusal ----------------------


def test_no_meta_means_no_writability_check() -> None:
    # Back-compat: callers that don't pass fields_meta keep old behaviour.
    out = validate_write_values("sale.order", {"display_name": "x"}, _KNOWN)
    assert out == {"display_name": "x"}


def test_missing_store_key_is_treated_as_stored() -> None:
    # A trimmed fields_get (no 'store' attribute) must not trigger a refusal.
    meta = {"weird": {"type": "char", "readonly": True}}
    out = validate_write_values("m", {"weird": 1}, frozenset({"weird"}), fields_meta=meta)
    assert out == {"weird": 1}


def test_field_absent_from_meta_is_not_refused() -> None:
    out = validate_write_values("m", {"ghost": 1}, frozenset({"ghost"}), fields_meta={})
    assert out == {"ghost": 1}


# -- recomputed_write_fields --------------------------------------------------


def test_recomputed_lists_only_stored_readonly() -> None:
    names = ["name", "amount_total", "note", "id"]
    assert recomputed_write_fields(_SALE_ORDER_FIELDS, names) == ["amount_total", "id"]


def test_recomputed_empty_without_meta() -> None:
    assert recomputed_write_fields(None, ["amount_total"]) == []


# -- dispatcher wiring --------------------------------------------------------


def test_write_refuses_non_stored_computed_before_reaching_odoo(tmp_path: Path) -> None:
    app, fake = _build_app(tmp_path)
    payload = _call(
        Dispatcher(app),
        "odoo_write",
        {
            "instance": "dev",
            "model": "sale.order",
            "ids": [1],
            "values": {"display_name": "Renamed"},
            "dry_run": False,
        },
    )
    assert "error" in payload
    assert "computed and not stored" in json.dumps(payload)
    # The important half: nothing was sent to Odoo.
    assert fake.write_calls == []


def test_create_refuses_non_stored_computed(tmp_path: Path) -> None:
    app, fake = _build_app(tmp_path)
    payload = _call(
        Dispatcher(app),
        "odoo_create",
        {
            "instance": "dev",
            "model": "sale.order",
            "values": {"name": "SO1", "display_name": "x"},
            "dry_run": False,
        },
    )
    assert "error" in payload
    assert fake.create_calls == []


def test_write_commit_reports_stored_readonly_fields(tmp_path: Path) -> None:
    app, fake = _build_app(tmp_path)
    payload = _call(
        Dispatcher(app),
        "odoo_write",
        {
            "instance": "dev",
            "model": "sale.order",
            "ids": [1],
            "values": {"name": "SO1", "amount_total": 99.0},
            "dry_run": False,
        },
    )
    assert payload["committed"] is True
    assert payload["readonly_fields_written"] == ["amount_total"]
    assert "recomputes" in payload["readonly_fields_note"]
    # Not a refusal — the write still went through.
    assert fake.write_calls[0][2] == {"name": "SO1", "amount_total": 99.0}


def test_write_dry_run_reports_stored_readonly_fields(tmp_path: Path) -> None:
    app, _ = _build_app(tmp_path)
    payload = _call(
        Dispatcher(app),
        "odoo_write",
        {
            "instance": "dev",
            "model": "sale.order",
            "ids": [1],
            "values": {"amount_total": 99.0},
            "dry_run": True,
        },
    )
    assert payload["preview"] is True
    assert payload["readonly_fields_written"] == ["amount_total"]


def test_clean_write_carries_no_warning_keys(tmp_path: Path) -> None:
    app, _ = _build_app(tmp_path)
    payload = _call(
        Dispatcher(app),
        "odoo_write",
        {
            "instance": "dev",
            "model": "sale.order",
            "ids": [1],
            "values": {"name": "SO1"},
            "dry_run": False,
        },
    )
    assert payload["committed"] is True
    assert "readonly_fields_written" not in payload
    assert "readonly_fields_note" not in payload


# -- nested (x2many command) values go through the same check ----------------


_ORDER_LINE_FIELDS: dict[str, dict[str, Any]] = {
    "id": {"type": "integer", "readonly": True, "store": True},
    "name": {"type": "char", "readonly": False, "store": True},
    # non-stored computed, no inverse: Odoo accepts and discards
    "display_name": {"type": "char", "readonly": True, "store": False},
}


class _PerModelClient(_FakeClient):
    """``fields_get`` keyed by model, so the nested target has its own schema."""

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        if model == "sale.order.line":
            return _ORDER_LINE_FIELDS
        return {
            **_SALE_ORDER_FIELDS,
            "order_line": {"type": "one2many", "relation": "sale.order.line"},
        }


def test_nested_create_refuses_non_stored_computed(tmp_path: Path) -> None:
    """``order_line=[(0, 0, {...})]`` is validated against sale.order.line's
    own ``fields_get`` — the relational-write gate already fetches that
    metadata, so the never-persisted check must run there too."""
    app, _ = _build_app(tmp_path)
    fake = _PerModelClient(_SALE_ORDER_FIELDS)
    app.instances["dev"].client = fake  # type: ignore[assignment]
    payload = _call(
        Dispatcher(app),
        "odoo_write",
        {
            "instance": "dev",
            "model": "sale.order",
            "ids": [1],
            "values": {"order_line": [(0, 0, {"name": "line", "display_name": "x"})]},
            "dry_run": False,
        },
    )
    assert "error" in payload
    assert "'display_name' on 'sale.order.line'" in json.dumps(payload)
    assert fake.write_calls == []
