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
    cfg = _instance_with_override({"account.move": ("id", "name", "partner_id", "amount_total")})
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


# ---------------------------------------------------------------------------
# A bad override entry is the operator's mistake, not the caller's.
#
# The caller omitted ``fields``; the refused field came from
# ``[instances.NAME.smart_fields_overrides.MODEL]``. A plain FieldPolicyError
# reads as if the caller asked for it — and for a sensitive entry it tells the
# caller to unlock via ``allow_sensitive_fields``, turning a plain read into a
# sensitive one nobody requested. The refusal must name the config key and
# point at the bypass that needs no unlock (an explicit ``fields=`` list).
# ---------------------------------------------------------------------------

_OVERRIDE_KEY = "[instances.dev.smart_fields_overrides.res.partner]"


def test_override_typo_names_the_config_key_not_the_caller(tmp_path: Path) -> None:
    fields_meta = {"id": {"type": "integer"}, "name": {"type": "char"}}
    fake = _FakeClient(fields_meta)
    cfg = _instance_with_override({"res.partner": ("id", "name", "emial")})
    payload = _call(
        Dispatcher(_build(tmp_path, fake, cfg)), {"instance": "dev", "model": "res.partner"}
    )
    assert payload["ok"] is False
    # Still a field-policy refusal — clients keying on the code see no change.
    assert payload["error_code"] == "field_policy"
    assert _OVERRIDE_KEY in payload["error"]
    assert "not your request" in payload["error"]
    # The underlying reason is preserved verbatim.
    assert "Field 'emial' does not exist on model 'res.partner'." in payload["error"]
    # The hint names the bypass that needs no unlock and no guessing.
    assert "fields=[...]" in payload["hint"]
    assert "smart_fields_overrides" in payload["hint"]
    # Nothing was sent to Odoo.
    assert fake.last_fields is None


def test_override_sensitive_field_is_attributed_and_not_an_unlock_invitation(
    tmp_path: Path,
) -> None:
    """The v0.15.0 promise: overrides cannot leak VAT / passwords / etc.

    On pristine main the refusal told the caller to pass
    ``allow_sensitive_fields=['vat']`` — and an obedient caller then reads
    VAT on a call where nobody asked for it. The refusal must say the
    field came from config and the hint must steer away from unlocking.
    """
    fields_meta = {"id": {"type": "integer"}, "name": {"type": "char"}, "vat": {"type": "char"}}
    fake = _FakeClient(fields_meta)
    cfg = _instance_with_override({"res.partner": ("id", "name", "vat")})
    payload = _call(
        Dispatcher(_build(tmp_path, fake, cfg)), {"instance": "dev", "model": "res.partner"}
    )
    assert payload["ok"] is False
    assert payload["error_code"] == "field_policy"
    assert _OVERRIDE_KEY in payload["error"]
    assert "'vat'" in payload["error"]
    assert payload["hint"].startswith("Do not unlock")
    assert "fields=[...]" in payload["hint"]
    assert fake.last_fields is None


def test_override_error_reaches_odoo_read_too(tmp_path: Path) -> None:
    """Both consumers of ``_resolve_read_fields`` refuse the same way."""
    fields_meta = {"id": {"type": "integer"}, "name": {"type": "char"}}
    fake = _FakeClient(fields_meta)
    cfg = _instance_with_override({"res.partner": ("id", "emial")})
    contents = asyncio.run(
        Dispatcher(_build(tmp_path, fake, cfg)).call(
            "odoo_read", {"instance": "dev", "model": "res.partner", "ids": [1]}
        )
    )
    payload = json.loads(contents[0].text)
    assert payload["ok"] is False
    assert payload["error_code"] == "field_policy"
    assert _OVERRIDE_KEY in payload["error"]
    assert "fields=[...]" in payload["hint"]


def test_override_error_is_recorded_in_the_audit_log_with_attribution(tmp_path: Path) -> None:
    """The operator reads the audit log, not the tool response — it must
    carry the config key so the entry is actionable there too."""
    fields_meta = {"id": {"type": "integer"}, "name": {"type": "char"}}
    fake = _FakeClient(fields_meta)
    cfg = _instance_with_override({"res.partner": ("id", "emial")})
    app = _build(tmp_path, fake, cfg)
    _call(Dispatcher(app), {"instance": "dev", "model": "res.partner"})
    lines = [json.loads(ln) for ln in app.config.audit_log_path.read_text().splitlines() if ln]
    failures = [ev for ev in lines if ev.get("result") == "field_policy"]
    assert failures, lines
    assert failures[-1]["tool"] == "odoo_search_read"
    assert _OVERRIDE_KEY in failures[-1]["details"]["error"]


def test_override_error_is_a_field_policy_error() -> None:
    from odoo_mcp.errors import FieldPolicyError, SmartFieldsOverrideError

    inner = FieldPolicyError("Field 'x' does not exist on model 'm'.")
    exc = SmartFieldsOverrideError("dev", "m", inner)
    assert isinstance(exc, FieldPolicyError)
    assert exc.code == "field_policy"
    assert exc.instance_name == "dev" and exc.model == "m"
    assert "[instances.dev.smart_fields_overrides.m]" in exc.user_message
    assert inner.user_message in exc.user_message
    assert exc.hint and "fields=[...]" in exc.hint


def test_explicit_fields_typo_is_not_attributed_to_config(tmp_path: Path) -> None:
    """Guard (passes before and after the fix): when the CALLER names the bad
    field, the refusal stays the plain one — even with an override configured
    for the same model. Attribution is only for fields the caller never named."""
    fields_meta = {"id": {"type": "integer"}, "name": {"type": "char"}}
    fake = _FakeClient(fields_meta)
    cfg = _instance_with_override({"res.partner": ("id", "name")})
    payload = _call(
        Dispatcher(_build(tmp_path, fake, cfg)),
        {"instance": "dev", "model": "res.partner", "fields": ["id", "emial"]},
    )
    assert payload["ok"] is False
    assert payload["error_code"] == "field_policy"
    assert payload["error"] == "Field 'emial' does not exist on model 'res.partner'."
    assert "smart_fields_overrides" not in payload["error"]
    assert "hint" not in payload
