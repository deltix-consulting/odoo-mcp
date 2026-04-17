"""Tests for credential loading, env purging, and redaction."""

from __future__ import annotations

import os

import pytest

from odoo_mcp.credentials import Credentials, load_credentials
from odoo_mcp.errors import CredentialsError, OdooAuthError, OdooMcpError, redact


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Strip any pre-existing test env so each test starts fresh.
    for key in list(os.environ):
        if key.startswith("ODOO_MCP_TEST_"):
            monkeypatch.delenv(key, raising=False)


def test_load_credentials_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_MCP_TEST_USERNAME", "me@example.com")
    monkeypatch.setenv("ODOO_MCP_TEST_API_KEY", "super-secret-value-1234")
    creds = load_credentials("testinst", "ODOO_MCP_TEST")
    assert creds.username == "me@example.com"
    assert creds.reveal_for_rpc() == "super-secret-value-1234"


def test_load_credentials_purges_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_MCP_TEST_USERNAME", "me@example.com")
    monkeypatch.setenv("ODOO_MCP_TEST_API_KEY", "super-secret-value-1234")
    load_credentials("testinst", "ODOO_MCP_TEST")
    # Both env vars must be gone — not just emptied — after loading.
    assert "ODOO_MCP_TEST_USERNAME" not in os.environ
    assert "ODOO_MCP_TEST_API_KEY" not in os.environ


def test_load_credentials_fails_closed_on_missing() -> None:
    # Neither variable set — the autouse _clean_env fixture strips any stray state.
    with pytest.raises(CredentialsError, match="Missing required"):
        load_credentials("testinst", "ODOO_MCP_TEST")


def test_load_credentials_fails_closed_on_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_MCP_TEST_USERNAME", "me@example.com")
    # API key missing.
    with pytest.raises(CredentialsError, match="ODOO_MCP_TEST_API_KEY"):
        load_credentials("testinst", "ODOO_MCP_TEST")


def test_credentials_repr_does_not_leak_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_MCP_TEST_USERNAME", "me@example.com")
    monkeypatch.setenv("ODOO_MCP_TEST_API_KEY", "super-secret-value-5678")
    creds = load_credentials("testinst", "ODOO_MCP_TEST")
    assert "super-secret" not in repr(creds)
    assert "super-secret" not in str(creds)
    assert "<redacted>" in repr(creds)


def test_error_redaction_scrubs_registered_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_MCP_TEST_USERNAME", "me@example.com")
    monkeypatch.setenv("ODOO_MCP_TEST_API_KEY", "hunter2-very-long-key-0987654321")
    load_credentials("testinst", "ODOO_MCP_TEST")

    # An error that accidentally embeds the secret should still be scrubbed
    # when stringified.
    err = OdooAuthError("Auth failed with body 'bad key hunter2-very-long-key-0987654321 oops'")
    s = str(err)
    assert "hunter2-very-long-key-0987654321" not in s
    assert "<redacted>" in s


def test_redact_helper_ignores_short_secrets() -> None:
    # Very short "secrets" would match everything if registered; we refuse to
    # register them. redact() on unrelated text must return the text unchanged.
    assert redact("The quick brown fox") == "The quick brown fox"


def test_credentials_instance_is_frozen() -> None:
    creds = Credentials(instance_name="x", username="u", _api_key="k" * 10)
    with pytest.raises(AttributeError):
        creds.username = "other"  # type: ignore[misc]


def test_odoo_mcp_error_base_is_stringable() -> None:
    assert str(OdooMcpError("boom")) == "boom"
