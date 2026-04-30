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


def test_secrets_registry_is_lru_bounded() -> None:
    """The secret registry caps growth at _SECRETS_MAX entries (LRU)."""
    from odoo_mcp.errors import _SECRETS, _SECRETS_MAX, register_secret

    # Snapshot + restore so we don't pollute other tests.
    snapshot = list(_SECRETS.keys())
    _SECRETS.clear()
    try:
        # Push more than the cap; the oldest entries must be evicted.
        for i in range(_SECRETS_MAX + 25):
            register_secret(f"secret-{i:04d}-padded-to-min-length")
        assert len(_SECRETS) == _SECRETS_MAX, "registry must not grow past cap"
        # The very first entries should have been evicted.
        assert "secret-0000-padded-to-min-length" not in _SECRETS
        # The last-inserted ones should still be there.
        assert f"secret-{_SECRETS_MAX + 24:04d}-padded-to-min-length" in _SECRETS
    finally:
        from odoo_mcp import errors as _err

        _SECRETS.clear()
        for s in snapshot:
            _SECRETS[s] = None
        _err._SECRETS_PATTERN = None


def test_redaction_uses_compiled_regex_and_scrubs_new_secrets() -> None:
    """A newly registered secret is picked up on the next redact() call.

    Validates the lazy-recompile behaviour of the cached pattern.
    """
    from odoo_mcp.errors import _SECRETS, register_secret

    snapshot = list(_SECRETS.keys())
    _SECRETS.clear()
    try:
        register_secret("first-rotation-key-abcdef")
        assert "<redacted>" in redact("the api key first-rotation-key-abcdef leaked")
        # Register a second secret AFTER the pattern was first compiled.
        register_secret("second-rotation-key-ghijkl")
        out = redact("two leaks: first-rotation-key-abcdef and second-rotation-key-ghijkl")
        assert "first-rotation-key-abcdef" not in out
        assert "second-rotation-key-ghijkl" not in out
        assert out.count("<redacted>") == 2
    finally:
        from odoo_mcp import errors as _err

        _SECRETS.clear()
        for s in snapshot:
            _SECRETS[s] = None
        _err._SECRETS_PATTERN = None


def test_credentials_instance_is_frozen() -> None:
    creds = Credentials(instance_name="x", username="u", _api_key="k" * 10)
    with pytest.raises(AttributeError):
        creds.username = "other"  # type: ignore[misc]


def test_odoo_mcp_error_base_is_stringable() -> None:
    assert str(OdooMcpError("boom")) == "boom"
