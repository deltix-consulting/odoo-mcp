"""Tests for lazy credential loading on :class:`OdooClient`.

The security property under test: a broken credential config for ONE instance
must not prevent the MCP from starting. Only calls to that instance should
fail, and only on first use.
"""

from __future__ import annotations

import pytest

from odoo_mcp.client import OdooClient
from odoo_mcp.config import InstanceConfig
from odoo_mcp.credentials import Credentials
from odoo_mcp.errors import CredentialsError, OdooAuthError


def _make_instance_config() -> InstanceConfig:
    return InstanceConfig(
        name="dev",
        url="https://dev.example.odoo.com",
        database="dev_db",
        credentials_env_prefix="ODOO_MCP_DEV",
        production=False,
        timeout_seconds=30,
        max_records_default=50,
        max_records_hard_cap=500,
        rate_limit_per_minute=300,
        allow_self_signed=False,
        allowed_models=frozenset({"res.partner"}),
    )


def test_client_constructs_with_broken_loader() -> None:
    """A loader that raises must NOT prevent client construction."""

    def _bad_loader() -> Credentials:
        raise CredentialsError("env var missing")

    cfg = _make_instance_config()
    # Construction succeeds even though the loader would fail.
    client = OdooClient(cfg, credential_loader=_bad_loader)
    assert client is not None


def test_client_raises_on_first_use_with_broken_loader() -> None:
    """The loader runs on first use. Its exception propagates."""

    def _bad_loader() -> Credentials:
        raise CredentialsError("env var missing")

    cfg = _make_instance_config()
    client = OdooClient(cfg, credential_loader=_bad_loader)
    with pytest.raises(CredentialsError, match="env var missing"):
        client.authenticate()


def test_client_requires_credentials_or_loader() -> None:
    cfg = _make_instance_config()
    with pytest.raises(OdooAuthError):
        OdooClient(cfg)


def test_client_caches_loaded_credentials() -> None:
    """The loader must only run once; subsequent uses hit the cache."""
    call_count = {"n": 0}

    def _counting_loader() -> Credentials:
        call_count["n"] += 1
        return Credentials(instance_name="dev", username="u", _api_key="k" * 10)

    cfg = _make_instance_config()
    client = OdooClient(cfg, credential_loader=_counting_loader)
    # Pull creds twice via the internal getter (authenticate would try to
    # hit the network). Two accesses, one loader invocation.
    client._get_credentials()  # type: ignore[attr-defined]
    client._get_credentials()  # type: ignore[attr-defined]
    assert call_count["n"] == 1
