"""Shared test fixtures.

The path-injection block exists so ``uv run pytest`` works from a fresh
checkout without an install step. The fixture builders below cut down
on the boilerplate that was being copy-pasted into every dispatcher
test (build a config + creds + client + app + dispatcher).

Tests that need a customized fake client should still write their own
— these helpers cover the common case where you just need a working
``OdooMcpApp`` to call the dispatcher against. A wildcard allowlist
and a non-prod instance are the only assumed defaults.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def make_instance_config() -> Callable[..., Any]:
    """Factory for an ``InstanceConfig`` with sensible defaults.

    Override any field by keyword. Returns the config; doesn't write any
    files. Works for both wildcard-allowlist and strict-list tests.
    """
    from odoo_mcp.config import InstanceConfig
    from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD

    def _make(**overrides: Any) -> InstanceConfig:
        defaults: dict[str, Any] = {
            "name": "dev",
            "url": "https://example.odoo.com",
            "database": "db",
            "credentials_env_prefix": "ODOO_MCP_DEV",
            "production": False,
            "timeout_seconds": 30,
            "max_records_default": 50,
            "max_records_hard_cap": 500,
            "rate_limit_per_minute": 300,
            "allow_self_signed": False,
            "allowed_models": frozenset({ALLOWLIST_WILDCARD}),
        }
        defaults.update(overrides)
        return InstanceConfig(**defaults)

    return _make


@pytest.fixture
def make_app(
    tmp_path: Path,
    make_instance_config: Callable[..., Any],
) -> Callable[..., Any]:
    """Factory for an ``OdooMcpApp`` with one instance.

    ``client`` overrides the default real ``OdooClient`` — pass a fake
    that exposes ``ensure_authenticated`` / ``fields_get`` / whatever
    the test needs. ``inst_cfg`` overrides the default config; if
    omitted, ``make_instance_config(**inst_overrides)`` is used.
    """
    from odoo_mcp.audit import AuditLog
    from odoo_mcp.client import OdooClient
    from odoo_mcp.config import AppConfig, Defaults
    from odoo_mcp.credentials import Credentials
    from odoo_mcp.dispatcher import InstanceRuntime, OdooMcpApp
    from odoo_mcp.security.limits import RateLimiter
    from odoo_mcp.security.prod_guard import ProdGuard

    def _make(
        *,
        client: Any | None = None,
        inst_cfg: Any | None = None,
        **inst_overrides: Any,
    ) -> OdooMcpApp:
        if inst_cfg is None:
            inst_cfg = make_instance_config(**inst_overrides)
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
        if client is not None:
            rt.client = client
        return OdooMcpApp(
            config=app_cfg,
            audit=AuditLog(app_cfg.audit_log_path),
            prod_guard=ProdGuard(),
            rate_limiter=rl,
            instances={inst_cfg.name: rt},
        )

    return _make
