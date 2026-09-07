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
# Real-home write guard
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_real_home_config_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Fail any test that writes one of our config files into the real home.

    ``setup_wizard._atomic_write_text`` is the single choke point for every
    config file this project writes: ``~/.odoo-mcp/config.toml``, the Claude
    Desktop JSON, ``~/.codex/config.toml`` and the onboarding suggestions
    file. Those destinations are module-level constants resolved from
    ``Path.home()`` / ``$CODEX_HOME`` **at import time**, so a test that
    drives a flow reaching ``_register_codex`` or ``_register_claude_desktop``
    hits the developer's real files — setting ``HOME`` inside the test cannot
    redirect a constant that was bound before the test started. The only
    defences are stubbing the call or this guard.

    The guard turns a silent mutation of the developer's machine into a test
    failure. Writes aimed at pytest's own temp tree are left alone, so tests
    that legitimately point the constants at ``tmp_path`` are unaffected.
    """
    from odoo_mcp import setup_wizard

    real_home = Path.home().resolve()
    basetemp = tmp_path_factory.getbasetemp().resolve()
    original = setup_wizard._atomic_write_text

    def _guarded(target: Path, content: str, *, mode: int = 0o600) -> None:
        resolved = Path(target).expanduser().resolve()
        under_home = resolved == real_home or real_home in resolved.parents
        under_tmp = resolved == basetemp or basetemp in resolved.parents
        if under_home and not under_tmp:
            msg = (
                f"test wrote to the real home directory: {resolved}\n"
                f"Stub the call (see _stub_update_preconditions in "
                f"tests/test_attestation.py) or point the destination at "
                f"tmp_path. Config destinations in setup_wizard are "
                f"module-level constants, so monkeypatching HOME will not "
                f"redirect them."
            )
            raise AssertionError(msg)
        original(target, content, mode=mode)

    monkeypatch.setattr(setup_wizard, "_atomic_write_text", _guarded)


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
