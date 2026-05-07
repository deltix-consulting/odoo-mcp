"""Tests for per-model smart_fields_overrides in config + dispatcher."""

from __future__ import annotations

import asyncio
import json
import textwrap
from pathlib import Path
from typing import Any

import pytest

from odoo_mcp.audit import AuditLog
from odoo_mcp.client import OdooClient
from odoo_mcp.config import AppConfig, ConfigError, Defaults, InstanceConfig, load_config
from odoo_mcp.credentials import Credentials
from odoo_mcp.dispatcher import Dispatcher, InstanceRuntime, OdooMcpApp
from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD
from odoo_mcp.security.limits import RateLimiter
from odoo_mcp.security.prod_guard import ProdGuard


def _write_config(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(textwrap.dedent(body))
    cfg.chmod(0o600)
    return cfg


def test_config_accepts_smart_fields_overrides(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path,
        """
        [defaults]
        audit_log = "{audit}"
        fields_cache_path = ""

        [instances.dev]
        url = "https://example.odoo.com"
        database = "db"
        credentials_env_prefix = "ODOO_MCP_DEV"
        production = false
        rate_limit_per_minute = 100
        allowed_models = ["*"]

        [instances.dev.smart_fields_overrides]
        "account.move" = ["id", "name", "partner_id", "amount_total"]
        """.replace("{audit}", str(tmp_path / "a.jsonl")),
    )
    cfg = load_config(cfg_path)
    inst = cfg.instances["dev"]
    assert inst.smart_fields_overrides["account.move"] == (
        "id",
        "name",
        "partner_id",
        "amount_total",
    )


def test_config_rejects_empty_override_list(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path,
        """
        [defaults]
        audit_log = "{audit}"
        fields_cache_path = ""

        [instances.dev]
        url = "https://example.odoo.com"
        database = "db"
        credentials_env_prefix = "ODOO_MCP_DEV"
        production = false
        rate_limit_per_minute = 100
        allowed_models = ["*"]

        [instances.dev.smart_fields_overrides]
        "account.move" = []
        """.replace("{audit}", str(tmp_path / "a.jsonl")),
    )
    with pytest.raises(ConfigError, match="non-empty list"):
        load_config(cfg_path)


def test_config_rejects_dotted_field_in_override(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path,
        """
        [defaults]
        audit_log = "{audit}"
        fields_cache_path = ""

        [instances.dev]
        url = "https://example.odoo.com"
        database = "db"
        credentials_env_prefix = "ODOO_MCP_DEV"
        production = false
        rate_limit_per_minute = 100
        allowed_models = ["*"]

        [instances.dev.smart_fields_overrides]
        "account.move" = ["id", "partner_id.name"]
        """.replace("{audit}", str(tmp_path / "a.jsonl")),
    )
    with pytest.raises(ConfigError, match="dotted"):
        load_config(cfg_path)


# ---------------------------------------------------------------------------
# Dispatcher integration
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, fields_meta: dict[str, dict[str, Any]]) -> None:
        self._fields_meta = fields_meta
        self.last_fields: list[str] | None = None
        self.is_admin: bool | None = None
        self.admin_reason: str | None = None
        self.username = "u"

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
        self.last_fields = list(fields)
        return [{"id": 1, **{f: f"val_{f}" for f in fields if f != "id"}}]


def _instance_with_override(
    overrides: dict[str, tuple[str, ...]],
) -> InstanceConfig:
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
        smart_fields_overrides=overrides,
    )


def _build(tmp_path: Path, fake: _FakeClient, inst_cfg: InstanceConfig) -> OdooMcpApp:
    creds = Credentials(instance_name=inst_cfg.name, username="u", _api_key="k" * 10)
    real = OdooClient(inst_cfg, credentials=creds)
    app_cfg = AppConfig(
        path=tmp_path / "config.toml",
        defaults=Defaults(),
        instances={inst_cfg.name: inst_cfg},
        audit_log_path=tmp_path / "audit.jsonl",
    )
    rl = RateLimiter()
    rl.configure(inst_cfg.name, inst_cfg.rate_limit_per_minute)
    rt = InstanceRuntime(config=inst_cfg, client=real)
    rt.client = fake  # type: ignore[assignment]
    return OdooMcpApp(
        config=app_cfg,
        audit=AuditLog(app_cfg.audit_log_path),
        prod_guard=ProdGuard(),
        rate_limiter=rl,
        instances={inst_cfg.name: rt},
    )


def _call(disp: Dispatcher, args: dict[str, Any]) -> dict[str, Any]:
    contents = asyncio.run(disp.call("odoo_search_read", args))
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


def test_override_replaces_smart_default(tmp_path: Path) -> None:
    fields_meta = {
        "id": {"type": "integer"},
        "name": {"type": "char"},
        "partner_id": {"type": "many2one"},
        "amount_total": {"type": "float"},
        "useless": {"type": "char"},
    }
    fake = _FakeClient(fields_meta)
    cfg = _instance_with_override(
        {"account.move": ("id", "name", "partner_id", "amount_total")}
    )
    app = _build(tmp_path, fake, cfg)
    payload = _call(Dispatcher(app), {"instance": "dev", "model": "account.move"})
    assert payload["ok"] is True
    assert payload["smart_fields_used"] == [
        "id",
        "name",
        "partner_id",
        "amount_total",
    ]
    # `useless` was excluded by the override.
    assert "useless" not in fake.last_fields  # type: ignore[operator]


def test_override_only_fires_when_fields_omitted(tmp_path: Path) -> None:
    fields_meta = {"id": {"type": "integer"}, "name": {"type": "char"}}
    fake = _FakeClient(fields_meta)
    cfg = _instance_with_override({"res.partner": ("id",)})
    app = _build(tmp_path, fake, cfg)
    # Caller passes explicit fields — the override must NOT kick in.
    payload = _call(
        Dispatcher(app),
        {"instance": "dev", "model": "res.partner", "fields": ["id", "name"]},
    )
    assert "smart_fields_used" not in payload
    assert fake.last_fields == ["id", "name"]


def test_override_with_unknown_field_surfaces_clear_error(tmp_path: Path) -> None:
    fields_meta = {"id": {"type": "integer"}, "name": {"type": "char"}}
    fake = _FakeClient(fields_meta)
    cfg = _instance_with_override({"res.partner": ("id", "ghost_field")})
    app = _build(tmp_path, fake, cfg)
    payload = _call(Dispatcher(app), {"instance": "dev", "model": "res.partner"})
    assert payload["ok"] is False
    assert "ghost_field" in payload["error"]


def test_other_models_use_smart_default_when_only_one_override(tmp_path: Path) -> None:
    fields_meta = {
        "id": {"type": "integer"},
        "name": {"type": "char"},
        "email": {"type": "char"},
    }
    fake = _FakeClient(fields_meta)
    cfg = _instance_with_override({"account.move": ("id",)})
    app = _build(tmp_path, fake, cfg)
    # No override for res.partner — falls back to smart selection.
    payload = _call(Dispatcher(app), {"instance": "dev", "model": "res.partner"})
    used = payload["smart_fields_used"]
    assert "name" in used
    assert "email" in used
