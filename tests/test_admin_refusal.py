"""Tests for admin-credential refusal on production instances.

The v0.5.0 default: a production instance authenticated as the Odoo
superuser (``uid=1``) or a member of ``base.group_system`` raises an
``OdooAuthError`` rather than silently letting per-user ACL scoping be
bypassed. Operators who knowingly need admin keys can opt out by setting
``refuse_admin_on_production = false`` in the instance's TOML config.
"""

from __future__ import annotations

from typing import Any

import pytest

from odoo_mcp.client import OdooClient
from odoo_mcp.config import InstanceConfig
from odoo_mcp.credentials import Credentials
from odoo_mcp.errors import OdooAuthError


def _make_cfg(*, production: bool, refuse_admin: bool = True) -> InstanceConfig:
    return InstanceConfig(
        name="prod" if production else "dev",
        url=("https://prod.example.odoo.com" if production else "https://dev.example.odoo.com"),
        database="db",
        credentials_env_prefix="ODOO_MCP_TEST",
        production=production,
        timeout_seconds=30,
        max_records_default=50,
        max_records_hard_cap=500,
        rate_limit_per_minute=60,
        allow_self_signed=False,
        allowed_models=frozenset({"res.partner"}),
        refuse_admin_on_production=refuse_admin,
    )


def _build_client(cfg: InstanceConfig, *, uid: int, has_group: bool) -> OdooClient:
    creds = Credentials(instance_name=cfg.name, username="u", _api_key="k" * 10)
    client = OdooClient(cfg, credentials=creds)

    # Stub out the XML-RPC layer so authenticate() never hits the network.
    class _StubCommon:
        def authenticate(self, *_args: Any, **_kw: Any) -> int:
            return uid

    def _execute(model: str, method: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
        if model == "res.users" and method == "has_group":
            return has_group
        raise AssertionError(f"unexpected execute_kw call: {model}.{method}")

    client._common = _StubCommon()  # type: ignore[assignment]
    client._execute = _execute  # type: ignore[assignment]
    return client


def test_admin_credentials_refused_on_production_by_default() -> None:
    cfg = _make_cfg(production=True)
    client = _build_client(cfg, uid=1, has_group=False)
    with pytest.raises(OdooAuthError) as excinfo:
        client.authenticate()
    msg = str(excinfo.value)
    assert "admin" in msg.lower()
    assert "refuse_admin_on_production" in msg
    assert "rotate-key" in msg


def test_admin_credentials_allowed_on_production_when_opted_out(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = _make_cfg(production=True, refuse_admin=False)
    client = _build_client(cfg, uid=1, has_group=False)
    with caplog.at_level("WARNING"):
        uid = client.authenticate()
    assert uid == 1
    assert client.is_admin is True
    # The opt-out path logs a warning so the choice isn't silent.
    assert any("opt-out" in rec.message.lower() for rec in caplog.records)


def test_admin_credentials_allowed_on_dev(caplog: pytest.LogCaptureFixture) -> None:
    cfg = _make_cfg(production=False)
    client = _build_client(cfg, uid=1, has_group=False)
    with caplog.at_level("WARNING"):
        uid = client.authenticate()
    assert uid == 1
    assert client.is_admin is True


def test_non_admin_user_passes() -> None:
    cfg = _make_cfg(production=True)
    client = _build_client(cfg, uid=2, has_group=False)
    uid = client.authenticate()
    assert uid == 2
    assert client.is_admin is False


def test_failed_auth_points_at_renew_key() -> None:
    """A falsy uid (wrong / expired key) yields a message that names the
    most likely Odoo Online cause and the exact command to fix it."""
    cfg = _make_cfg(production=True)
    # uid=0 (falsy) is what Odoo returns for a wrong OR expired key —
    # the two are indistinguishable at the authenticate() call.
    client = _build_client(cfg, uid=0, has_group=False)
    with pytest.raises(OdooAuthError) as excinfo:
        client.authenticate()
    msg = str(excinfo.value)
    assert "expired" in msg.lower()
    assert "renew-key prod" in msg
    assert "Odoo Online" in msg
