"""A ``readonly`` field that ``states`` unlocks is a plain column, not a computed one.

Odoo 16 and earlier declare document fields as::

    partner_id = fields.Many2one(..., readonly=True,
                                 states={'draft': [('readonly', False)]})

(``account.move.partner_id`` / ``invoice_date`` / ``invoice_line_ids`` /
``line_ids``, ``hr.expense.employee_id``, ``mrp.production.product_id``,
``stock.picking.picking_type_id``, ``sale.order.name`` — source-checked on
16.0). ``fields_get`` reports ``readonly: true`` for them, and Odoo's own
``Field.is_editable`` treats them as editable because a ``states`` entry
touches ``readonly``. Odoo 17 removed ``states`` and ``fields_get`` silently
ignores an attribute name it does not know, so on 17+ the key never appears.

Without ``states`` in the ``fields_get`` attribute list, every edit of a
draft vendor bill on 16.0 came back with ``readonly_fields_written`` and a
note that "the value is lost" — a warning for an ordinary, persisted write.
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
from odoo_mcp.fields_cache import _PAYLOAD_SCHEMA_VERSION, PersistentFieldsCache
from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD
from odoo_mcp.security.fields import (
    _states_unlock_readonly,
    recomputed_write_fields,
    validate_write_values,
)
from odoo_mcp.security.limits import RateLimiter
from odoo_mcp.security.prod_guard import ProdGuard

# account.move on Odoo 16.0 as fields_get returns it over XML-RPC (tuples
# arrive as lists).
_DRAFT_UNLOCK = {"draft": [["readonly", False]]}
_MOVE_16: dict[str, dict[str, Any]] = {
    "id": {"type": "integer", "readonly": True, "store": True},
    "ref": {"type": "char", "readonly": False, "store": True},
    "partner_id": {
        "type": "many2one",
        "relation": "res.partner",
        "readonly": True,
        "store": True,
        "states": _DRAFT_UNLOCK,
    },
    "invoice_date": {"type": "date", "readonly": True, "store": True, "states": _DRAFT_UNLOCK},
    # stored computed, no states: still reported
    "amount_total": {"type": "monetary", "readonly": True, "store": True},
    # non-stored computed, no inverse: still refused
    "display_name": {"type": "char", "readonly": True, "store": False},
}
_KNOWN = frozenset(_MOVE_16)


# -- the predicate -------------------------------------------------------------


@pytest.mark.parametrize(
    "states",
    [
        {"draft": [["readonly", False]]},
        {"draft": [("readonly", False)]},
        {"sent": [["required", True]], "draft": [["readonly", False], ["required", True]]},
    ],
)
def test_states_that_lift_readonly(states: Any) -> None:
    assert _states_unlock_readonly({"readonly": True, "states": states})


@pytest.mark.parametrize(
    "states",
    [
        None,
        {},
        "draft",
        [["readonly", False]],
        # a states entry that LOCKS (the sale.order READONLY_FIELD_STATES shape)
        {"sale": [["readonly", True]], "done": [["readonly", True]]},
        {"draft": [["required", True]]},
        {"draft": "readonly"},
        {"draft": [["readonly"]]},
    ],
)
def test_states_that_do_not_lift_readonly(states: Any) -> None:
    assert not _states_unlock_readonly({"readonly": True, "states": states})


# -- the two write-side checks -------------------------------------------------


def test_state_unlocked_stored_fields_are_not_reported() -> None:
    names = ["ref", "partner_id", "invoice_date", "amount_total"]
    assert recomputed_write_fields(_MOVE_16, names) == ["amount_total"]


def test_draft_bill_edit_on_16_carries_no_readonly_report() -> None:
    assert recomputed_write_fields(_MOVE_16, ["partner_id", "invoice_date"]) == []


def test_non_stored_state_unlocked_field_fails_open() -> None:
    meta = {
        "x_name": {"type": "char", "readonly": True, "store": False, "states": _DRAFT_UNLOCK},
    }
    # Odoo calls it editable; readonly alone is no proof the write is a no-op.
    assert validate_write_values("account.move", {"x_name": "a"}, frozenset(meta), fields_meta=meta)


def test_non_stored_computed_without_states_is_still_refused() -> None:
    with pytest.raises(FieldPolicyError, match="computed and not stored"):
        validate_write_values("account.move", {"display_name": "x"}, _KNOWN, fields_meta=_MOVE_16)


# -- end to end through odoo_write ---------------------------------------------


class _FakeClient:
    def __init__(self, fields: dict[str, dict[str, Any]]) -> None:
        self._fields = fields
        self.write_calls: list[tuple[str, list[int], dict[str, Any]]] = []
        self.is_admin: bool | None = None
        self.admin_reason: str | None = None

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True, **_: Any) -> dict[str, Any]:
        return self._fields

    def write(self, model: str, ids: list[int], values: dict[str, Any], **_: Any) -> bool:
        self.write_calls.append((model, ids, values))
        return True


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
    rt = InstanceRuntime(config=inst_cfg, client=OdooClient(inst_cfg, credentials=creds))
    fake = _FakeClient(_MOVE_16)
    rt.client = fake  # type: ignore[assignment]
    app_cfg = AppConfig(
        path=tmp_path / "config.toml",
        defaults=Defaults(),
        instances={inst_cfg.name: inst_cfg},
        audit_log_path=tmp_path / "audit.jsonl",
    )
    rate_limiter = RateLimiter()
    rate_limiter.configure(inst_cfg.name, inst_cfg.rate_limit_per_minute)
    app = OdooMcpApp(
        config=app_cfg,
        audit=AuditLog(app_cfg.audit_log_path),
        prod_guard=ProdGuard(),
        rate_limiter=rate_limiter,
        instances={inst_cfg.name: rt},
    )
    return app, fake


def _write(tmp_path: Path, values: dict[str, Any]) -> tuple[dict[str, Any], _FakeClient]:
    app, fake = _build_app(tmp_path)
    contents = asyncio.run(
        Dispatcher(app).call(
            "odoo_write",
            {
                "instance": "dev",
                "model": "account.move",
                "ids": [7],
                "values": values,
                "dry_run": False,
            },
        )
    )
    payload: dict[str, Any] = json.loads(contents[0].text)
    return payload, fake


def test_odoo_write_draft_bill_commits_without_warning(tmp_path: Path) -> None:
    payload, fake = _write(tmp_path, {"partner_id": 12, "invoice_date": "2026-11-18"})
    assert payload["committed"] is True
    assert "readonly_fields_written" not in payload
    assert "readonly_fields_note" not in payload
    assert fake.write_calls == [
        ("account.move", [7], {"partner_id": 12, "invoice_date": "2026-11-18"})
    ]


def test_odoo_write_still_reports_a_stored_computed_field(tmp_path: Path) -> None:
    payload, _ = _write(tmp_path, {"partner_id": 12, "amount_total": 5.0})
    assert payload["readonly_fields_written"] == ["amount_total"]


# -- the attribute is requested, and old cache rows are re-fetched --------------


def test_fields_get_requests_states(tmp_path: Path) -> None:
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
    client = OdooClient(
        inst_cfg, credentials=Credentials(instance_name="dev", username="u", _api_key="k" * 10)
    )
    client._uid = 1  # type: ignore[attr-defined]
    seen: list[dict[str, Any]] = []

    def _fake_execute(
        model: str, method: str, args: list[Any], kwargs: dict[str, Any], **_: Any
    ) -> Any:
        seen.append(kwargs)
        return {}

    client._execute = _fake_execute  # type: ignore[assignment]
    client.fields_get("account.move", use_cache=False)
    assert "states" in seen[0]["attributes"]
    assert "readonly" in seen[0]["attributes"]


def test_cache_rows_without_states_are_a_miss(tmp_path: Path) -> None:
    assert _PAYLOAD_SCHEMA_VERSION >= 3
    cache = PersistentFieldsCache(tmp_path / "fields.sqlite")
    cache.put("dev", "account.move", _MOVE_16)
    assert cache.get("dev", "account.move") == _MOVE_16
    with cache._connect() as conn:  # type: ignore[attr-defined]
        conn.execute(
            "UPDATE fields SET payload = ? WHERE instance = ? AND model = ?",
            (json.dumps({"_v": 2, "fields": _MOVE_16}), "dev", "account.move"),
        )
        conn.commit()
    assert cache.get("dev", "account.move") is None
