"""odoo_read_group must not ask Odoo for two values under one output name.

Odoo's legacy ``read_group`` names each aggregate after its field. On 17.0-19.0
the aggregates live in a dict keyed by that name and the result row is
``dict(zip(groupby_keys + aggregate_keys, row))``, so:

- ``amount_total:sum`` + ``amount_total:max`` computes only the max and
  returns it as ``amount_total`` (16.0 raises "Output name used twice");
- ``stage_id:count`` grouped by ``stage_id`` overwrites the group label with
  the count (16.0 drops the aggregate instead).

Both are silent wrong answers in a reporting tool, so the call is refused
before it reaches Odoo.
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
from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD
from odoo_mcp.security.fields import (
    FieldPolicyError,
    check_aggregate_groupby_overlap,
    validate_aggregate_fields,
)
from odoo_mcp.security.limits import RateLimiter
from odoo_mcp.security.prod_guard import ProdGuard

MOVE_FIELDS = frozenset({"id", "amount_total", "partner_id", "invoice_date", "state"})


# --- validate_aggregate_fields: one spec per output name ---------------------


@pytest.mark.parametrize(
    "specs",
    [
        ["amount_total:sum", "amount_total:max"],
        ["amount_total", "amount_total:sum"],
        ["amount_total:sum", "id:count", "amount_total:avg"],
    ],
)
def test_two_functions_on_one_field_are_refused(specs: list[str]) -> None:
    with pytest.raises(FieldPolicyError, match="amount_total") as exc:
        validate_aggregate_fields("account.move", specs, MOVE_FIELDS, allow_sensitive=frozenset())
    assert "second odoo_read_group call" in str(exc.value)


def test_same_spec_twice_is_not_a_collision() -> None:
    # Identical specs compute the same value, so nothing is lost.
    out = validate_aggregate_fields(
        "account.move",
        ["amount_total:sum", "amount_total:sum"],
        MOVE_FIELDS,
        allow_sensitive=frozenset(),
    )
    assert out == ["amount_total:sum", "amount_total:sum"]


def test_different_fields_still_pass() -> None:
    out = validate_aggregate_fields(
        "account.move",
        ["amount_total:sum", "id:count"],
        MOVE_FIELDS,
        allow_sensitive=frozenset(),
    )
    assert out == ["amount_total:sum", "id:count"]


# --- check_aggregate_groupby_overlap -----------------------------------------


@pytest.mark.parametrize(
    ("fields", "groupby", "lazy"),
    [
        (["partner_id:count"], ["partner_id"], True),
        (["partner_id:count_distinct"], ["state", "partner_id"], False),
        (["invoice_date:count"], ["invoice_date:month"], True),
    ],
)
def test_aggregate_on_a_grouped_field_is_refused(
    fields: list[str], groupby: list[str], lazy: bool
) -> None:
    with pytest.raises(FieldPolicyError, match="also a groupby field"):
        check_aggregate_groupby_overlap(fields, groupby, lazy=lazy)


@pytest.mark.parametrize(
    ("fields", "groupby", "lazy"),
    [
        # Bare name next to its own groupby: Odoo skips it, nothing collides.
        (["partner_id", "amount_total:sum"], ["partner_id"], True),
        # Lazy mode only groups by the first entry.
        (["partner_id:count_distinct"], ["state", "partner_id"], True),
        (["id:count", "amount_total:sum"], ["partner_id", "invoice_date:month"], False),
    ],
)
def test_no_overlap_passes(fields: list[str], groupby: list[str], lazy: bool) -> None:
    check_aggregate_groupby_overlap(fields, groupby, lazy=lazy)


# --- dispatcher: refused before any RPC --------------------------------------


class _RecordingClient:
    def __init__(self) -> None:
        self.read_group_calls = 0

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        return {
            "id": {"type": "integer"},
            "amount_total": {"type": "monetary"},
            "partner_id": {"type": "many2one", "relation": "res.partner"},
            "state": {"type": "selection"},
        }

    def read_group(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        self.read_group_calls += 1
        return []


def _dispatcher(tmp_path: Path, fake: _RecordingClient) -> Dispatcher:
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


def _call(dispatcher: Dispatcher, args: dict[str, Any]) -> dict[str, Any]:
    contents = asyncio.run(dispatcher.call("odoo_read_group", args))
    payload: dict[str, Any] = json.loads(contents[0].text)
    return payload


@pytest.mark.parametrize(
    ("fields", "groupby"),
    [
        (["amount_total:sum", "amount_total:max"], ["state"]),
        (["partner_id:count"], ["partner_id"]),
    ],
)
def test_dispatcher_refuses_before_odoo(
    tmp_path: Path, fields: list[str], groupby: list[str]
) -> None:
    fake = _RecordingClient()
    payload = _call(
        _dispatcher(tmp_path, fake),
        {"instance": "dev", "model": "account.move", "fields": fields, "groupby": groupby},
    )
    assert payload["ok"] is False
    assert fake.read_group_calls == 0


def test_dispatcher_still_runs_a_normal_report(tmp_path: Path) -> None:
    fake = _RecordingClient()
    payload = _call(
        _dispatcher(tmp_path, fake),
        {
            "instance": "dev",
            "model": "account.move",
            "fields": ["id:count", "amount_total:sum"],
            "groupby": ["partner_id"],
        },
    )
    assert payload["ok"] is True
    assert fake.read_group_calls == 1
