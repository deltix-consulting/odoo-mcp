"""Integration tests against a real Odoo instance.

Skipped by default. To run, set the following environment before
``pytest -m integration``::

    export ODOO_MCP_TEST_INSTANCE=dev
    export ODOO_MCP_TEST_URL="https://dev.example.odoo.com"
    export ODOO_MCP_TEST_DB="dev_db"
    export ODOO_MCP_TEST_USERNAME="..."
    export ODOO_MCP_TEST_API_KEY="..."

The test creates a temp partner, writes to it, reads it back with and without
an opt-in for ``vat``, verifies the field is redacted in the first case and
present in the second, and tears down by flipping ``active = False`` (since
we never expose unlink).
"""

from __future__ import annotations

import os

import pytest

from odoo_mcp.client import OdooClient
from odoo_mcp.config import InstanceConfig
from odoo_mcp.credentials import Credentials
from odoo_mcp.security.fields import redact_response

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def live_client() -> OdooClient:
    name = os.environ.get("ODOO_MCP_TEST_INSTANCE")
    if not name:
        pytest.skip("Set ODOO_MCP_TEST_INSTANCE + related env vars to run integration tests.")

    url = os.environ["ODOO_MCP_TEST_URL"]
    db = os.environ["ODOO_MCP_TEST_DB"]
    username = os.environ["ODOO_MCP_TEST_USERNAME"]
    api_key = os.environ["ODOO_MCP_TEST_API_KEY"]

    cfg = InstanceConfig(
        name=name,
        url=url.rstrip("/"),
        database=db,
        credentials_env_prefix="ODOO_MCP_TEST",
        production=False,
        timeout_seconds=30,
        max_records_default=50,
        max_records_hard_cap=500,
        rate_limit_per_minute=300,
        allow_self_signed=False,
        allowed_models=frozenset({"res.partner"}),
    )
    creds = Credentials(instance_name=name, username=username, _api_key=api_key)
    client = OdooClient(cfg, creds)
    client.authenticate()
    return client


def test_authenticate_and_fields_get(live_client: OdooClient) -> None:
    fg = live_client.fields_get("res.partner")
    assert "name" in fg
    assert "email" in fg
    # Password/token fields should never show up via the redaction layer,
    # but fields_get itself may still return them; the redactor removes them.
    # Here we just assert the raw call returned a dict.
    assert isinstance(fg, dict)


def test_create_write_read_redact(live_client: OdooClient) -> None:
    partner_id = live_client.create(
        "res.partner", {"name": "odoo-mcp test partner", "vat": "BE0123456789"}
    )
    try:
        records = live_client.read("res.partner", [partner_id], ["name", "vat"])
        field_types = {
            name: meta.get("type", "")
            for name, meta in live_client.fields_get("res.partner").items()
        }

        # Default redaction: vat stripped.
        stripped = redact_response(
            "res.partner",
            records,
            field_types,
            allow_sensitive=frozenset(),
            include_binary=False,
        )
        assert "vat" not in stripped[0]

        # Unlocked: vat visible.
        unlocked = redact_response(
            "res.partner",
            records,
            field_types,
            allow_sensitive=frozenset({"vat"}),
            include_binary=False,
        )
        assert unlocked[0]["vat"] == "BE0123456789"
    finally:
        # Soft-delete: flip active=False. We never expose unlink to the MCP.
        live_client.write("res.partner", [partner_id], {"active": False})
