"""odoo_describe_model must be able to answer from Odoo, not only from a snapshot.

``fields_get`` is cached at two levels — an in-process L1 dict that never
expires, and an on-disk L2 with a 24h TTL — and *every* field-name check in
the server (explicit ``fields``, domain leaves, groupby, aggregates, write
values) keys off that snapshot. A field added to Odoo after the snapshot is
therefore not just absent from ``odoo_describe_model``: reading it is refused
with "does not exist on model", a false statement about live Odoo, and the
refusal's own advice ("call odoo_describe_model") pointed straight back at the
same stale snapshot.

``OdooClient.fields_get`` has always taken ``use_cache=False``; nothing called
it. These tests pin the wiring, the recovery, and the disclosure.

Deliberately builds a *real* ``OdooClient`` over a stubbed ``_execute`` and a
*real* ``PersistentFieldsCache`` — a fake client would make both cache layers
disappear, which is exactly the code under test.
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
from odoo_mcp.errors import DomainSandboxError, FieldPolicyError
from odoo_mcp.fields_cache import PersistentFieldsCache
from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD
from odoo_mcp.security.limits import RateLimiter
from odoo_mcp.security.prod_guard import ProdGuard
from odoo_mcp.tools import build_tools

# The schema Odoo reports before, and after, someone adds a Studio field.
_BEFORE: dict[str, dict[str, Any]] = {
    "id": {"type": "integer", "string": "ID", "store": True},
    "name": {"type": "char", "string": "Name", "store": True},
}
_AFTER: dict[str, dict[str, Any]] = {
    **_BEFORE,
    "x_studio_loyalty_tier": {"type": "char", "string": "Loyalty Tier", "store": True},
}


class _StubTransport(OdooClient):
    """A real OdooClient — caches and all — with only the RPC boundary stubbed."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.schema: dict[str, dict[str, Any]] = dict(_BEFORE)
        self.fields_get_calls = 0

    def ensure_authenticated(self) -> None:
        return None

    def _execute(self, model: str, method: str, *args: Any, **kwargs: Any) -> Any:
        if method == "fields_get":
            self.fields_get_calls += 1
            return dict(self.schema)
        if method == "search_read":
            return [{"id": 1, "x_studio_loyalty_tier": "gold"}]
        raise AssertionError(f"unexpected RPC: {method}")


def _instance(tmp_path: Path) -> InstanceConfig:
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


def _build(tmp_path: Path) -> tuple[OdooMcpApp, _StubTransport, PersistentFieldsCache]:
    cfg = _instance(tmp_path)
    creds = Credentials(instance_name=cfg.name, username="u", _api_key="k" * 10)
    cache = PersistentFieldsCache(tmp_path / "fields-cache.db")
    client = _StubTransport(cfg, credentials=creds, fields_cache=cache)
    rl = RateLimiter()
    rl.configure(cfg.name, cfg.rate_limit_per_minute)
    app = OdooMcpApp(
        config=AppConfig(
            path=tmp_path / "config.toml",
            defaults=Defaults(),
            instances={cfg.name: cfg},
            audit_log_path=tmp_path / "audit.jsonl",
        ),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        prod_guard=ProdGuard(),
        rate_limiter=rl,
        instances={cfg.name: InstanceRuntime(config=cfg, client=client)},
    )
    return app, client, cache


def _describe(disp: Dispatcher, **extra: Any) -> dict[str, Any]:
    args = {"instance": "dev", "model": "res.partner", **extra}
    contents = asyncio.run(disp.call("odoo_describe_model", args))
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


def _audit_rows(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    # The log's first line is a non-event header with no ``tool`` key.
    return [r for r in rows if r.get("tool")]


def test_describe_model_without_refresh_answers_from_the_snapshot(tmp_path: Path) -> None:
    """The control: the default path is cached, and that is why refresh exists."""
    app, client, _ = _build(tmp_path)
    disp = Dispatcher(app)

    assert sorted(_describe(disp)["fields"]) == ["id", "name"]
    client.schema = dict(_AFTER)
    # Same process, Odoo has moved on, and the L1 dict has no TTL at all.
    assert sorted(_describe(disp)["fields"]) == ["id", "name"]
    assert client.fields_get_calls == 1


def test_refresh_rereads_the_schema_from_odoo(tmp_path: Path) -> None:
    app, client, _ = _build(tmp_path)
    disp = Dispatcher(app)

    _describe(disp)
    client.schema = dict(_AFTER)
    payload = _describe(disp, refresh=True)

    assert "x_studio_loyalty_tier" in payload["fields"]
    assert client.fields_get_calls == 2


def test_refresh_unblocks_the_read_that_the_stale_snapshot_refused(tmp_path: Path) -> None:
    """The whole point: a refused read must become possible without a restart."""
    app, client, _ = _build(tmp_path)
    disp = Dispatcher(app)

    _describe(disp)
    client.schema = dict(_AFTER)
    read_args = {
        "instance": "dev",
        "model": "res.partner",
        "domain": [],
        "fields": ["id", "x_studio_loyalty_tier"],
    }
    with pytest.raises(FieldPolicyError):
        disp._dispatch("odoo_search_read", dict(read_args))

    _describe(disp, refresh=True)

    # One refreshed describe repoints every later tool in the session.
    records = disp._dispatch("odoo_search_read", dict(read_args))["records"]
    assert records[0]["x_studio_loyalty_tier"] == "gold"


def test_refresh_writes_through_so_the_next_process_agrees(tmp_path: Path) -> None:
    """A refresh must not be undone by the 24h on-disk snapshot it bypassed."""
    app, client, cache = _build(tmp_path)
    disp = Dispatcher(app)

    _describe(disp)
    client.schema = dict(_AFTER)
    _describe(disp, refresh=True)

    # Restart: brand-new client and L1, same on-disk cache file.
    cfg = _instance(tmp_path)
    creds = Credentials(instance_name=cfg.name, username="u", _api_key="k" * 10)
    restarted = _StubTransport(cfg, credentials=creds, fields_cache=cache)
    assert "x_studio_loyalty_tier" in restarted.fields_get("res.partner")
    assert restarted.fields_get_calls == 0  # served from L2, not re-fetched


def test_unknown_field_refusal_names_the_escape_hatch(tmp_path: Path) -> None:
    """The refusal is only true of the snapshot — it must say so."""
    app, client, _ = _build(tmp_path)
    disp = Dispatcher(app)
    _describe(disp)
    client.schema = dict(_AFTER)

    with pytest.raises(FieldPolicyError, match="refresh=true"):
        disp._dispatch(
            "odoo_search_read",
            {
                "instance": "dev",
                "model": "res.partner",
                "domain": [],
                "fields": ["x_studio_loyalty_tier"],
            },
        )


def test_unknown_domain_field_refusal_names_the_escape_hatch(tmp_path: Path) -> None:
    """The domain sandbox said 'use odoo_describe_model' — the same stale snapshot."""
    app, client, _ = _build(tmp_path)
    disp = Dispatcher(app)
    _describe(disp)
    client.schema = dict(_AFTER)

    with pytest.raises(DomainSandboxError, match="refresh=true"):
        disp._dispatch(
            "odoo_search_read",
            {
                "instance": "dev",
                "model": "res.partner",
                "domain": [["x_studio_loyalty_tier", "=", "gold"]],
            },
        )


def test_unknown_order_field_refusal_names_the_escape_hatch(tmp_path: Path) -> None:
    """v0.27.0's ``validate_order`` is the sixth "does not exist" refusal."""
    app, client, _ = _build(tmp_path)
    disp = Dispatcher(app)
    _describe(disp)
    client.schema = dict(_AFTER)

    with pytest.raises(FieldPolicyError, match="refresh=true"):
        disp._dispatch(
            "odoo_search_read",
            {
                "instance": "dev",
                "model": "res.partner",
                "domain": [],
                "order": "x_studio_loyalty_tier desc",
            },
        )


def test_response_always_says_whether_the_schema_was_refreshed(tmp_path: Path) -> None:
    """Both values, always present — an unqualified schema reads as authoritative."""
    app, _, _ = _build(tmp_path)
    disp = Dispatcher(app)

    assert _describe(disp)["schema_refreshed"] is False
    assert _describe(disp, refresh=True)["schema_refreshed"] is True


def test_audit_row_records_whether_odoo_was_re_read(tmp_path: Path) -> None:
    """The extra round trip is an operator-visible cost; log it."""
    app, _, _ = _build(tmp_path)
    disp = Dispatcher(app)

    _describe(disp)
    _describe(disp, refresh=True)

    rows = [r for r in _audit_rows(app.config.audit_log_path) if r["tool"] == "odoo_describe_model"]
    assert [r["details"]["refresh"] for r in rows] == [False, True]


def test_tool_schema_exposes_refresh(tmp_path: Path) -> None:
    """additionalProperties is False — an unadvertised flag is unreachable."""
    tool = next(t for t in build_tools() if t.name == "odoo_describe_model")
    props = tool.inputSchema["properties"]
    assert props["refresh"]["type"] == "boolean"
    assert props["refresh"]["default"] is False
    assert tool.inputSchema["additionalProperties"] is False
