"""Tests for the ``--json`` flags on ``cache`` and ``status``.

Doctor's ``--json`` is covered in :mod:`tests.test_doctor`. These two
CLIs are minor — keep the tests thin.
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from odoo_mcp import cache_cli
from odoo_mcp.fields_cache import PersistentFieldsCache


def _capture(fn, *argv: str) -> tuple[int, str]:  # type: ignore[no-untyped-def]
    out = StringIO()
    with patch("sys.stdout", out):
        rc = fn(list(argv))
    return rc, out.getvalue()


# ---------------------------------------------------------------------------
# cache --info --json
# ---------------------------------------------------------------------------


def test_cache_info_json_emits_machine_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "fc.db"
    PersistentFieldsCache(db).put("dev", "res.partner", {"id": {"type": "integer"}})
    monkeypatch.setattr(cache_cli, "_resolve_cache_path", lambda: db)

    rc, out = _capture(cache_cli.main, "--info", "--json")
    assert rc == 0
    payload = json.loads(out.strip())
    assert "row_count" in payload
    assert payload["row_count"] >= 1
    assert "ttl_seconds" in payload


def test_cache_info_human_default_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "fc.db"
    PersistentFieldsCache(db)
    monkeypatch.setattr(cache_cli, "_resolve_cache_path", lambda: db)
    rc, out = _capture(cache_cli.main, "--info")
    assert rc == 0
    assert "rows:" in out
    assert "ttl:" in out


# ---------------------------------------------------------------------------
# status --json
# ---------------------------------------------------------------------------


def test_status_main_unknown_arg_returns_2() -> None:
    from odoo_mcp import status_cli

    err = StringIO()
    with patch("sys.stderr", err):
        rc = status_cli.main(["--bogus"])
    assert rc == 2
    assert "Usage" in err.getvalue() or "Unknown" in err.getvalue()


def test_status_payload_shape(tmp_path: Path) -> None:
    """Build an app directly and snapshot ``_status_payload``.

    Avoids the global config dependency that ``status_cli.main`` has.
    """
    from odoo_mcp.audit import AuditLog
    from odoo_mcp.client import OdooClient
    from odoo_mcp.config import AppConfig, Defaults, InstanceConfig
    from odoo_mcp.credentials import Credentials
    from odoo_mcp.dispatcher import InstanceRuntime, OdooMcpApp
    from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD
    from odoo_mcp.security.limits import RateLimiter
    from odoo_mcp.security.prod_guard import ProdGuard
    from odoo_mcp.status_cli import _status_payload

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
    client = OdooClient(cfg, credentials=creds)
    app_cfg = AppConfig(
        path=tmp_path / "config.toml",
        defaults=Defaults(),
        instances={cfg.name: cfg},
        audit_log_path=tmp_path / "audit.jsonl",
    )
    rl = RateLimiter()
    rl.configure(cfg.name, cfg.rate_limit_per_minute)
    app = OdooMcpApp(
        config=app_cfg,
        audit=AuditLog(app_cfg.audit_log_path),
        prod_guard=ProdGuard(),
        rate_limiter=rl,
        instances={cfg.name: InstanceRuntime(config=cfg, client=client)},
    )
    payload = _status_payload(app)
    assert payload["version"]
    assert payload["instances"]
    inst = payload["instances"][0]
    assert inst["name"] == "dev"
    assert inst["production"] is False
    assert inst["writes_unlocked"] is True  # non-prod
    assert "rate_limit" in inst
    assert inst["rate_limit"]["capacity_per_minute"] == 300
