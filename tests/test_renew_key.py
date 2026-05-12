"""Tests for ``odoo-mcp renew-key INSTANCE``.

The command is the daily-renewal flow for Odoo Online where non-admin
API keys expire after 1 day. It authenticates with the user's password
once, generates a fresh key, stores it, and discards the password.
These tests mock the XML-RPC layer so they run offline.
"""

from __future__ import annotations

import xmlrpc.client
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from odoo_mcp import setup_wizard

_CONFIG_BODY = """\
[defaults]
audit_log = "{audit}"
fields_cache_path = ""

[instances.prod]
url = "https://deltix.odoo.com"
database = "deltix"
credentials_env_prefix = "ODOO_MCP_PROD"
production = true
rate_limit_per_minute = 100
allowed_models = ["*"]
"""


@pytest.fixture
def fake_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(_CONFIG_BODY.replace("{audit}", str(tmp_path / "audit.jsonl")))
    cfg.chmod(0o600)
    monkeypatch.setattr(setup_wizard, "DEFAULT_CONFIG_PATH", cfg)
    monkeypatch.setattr(setup_wizard, "_CONFIG_DIR", tmp_path)
    return cfg


@pytest.fixture
def fake_keychain(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    store: dict[tuple[str, str], str] = {
        ("prod", "ODOO_MCP_PROD_USERNAME"): "timon@deltix.pro",
        ("prod", "ODOO_MCP_PROD_API_KEY"): "old-expired-key",
    }
    monkeypatch.setattr(
        setup_wizard,
        "_keychain_get",
        lambda inst, svc: store.get((inst, svc)),
    )
    monkeypatch.setattr(
        setup_wizard,
        "_keychain_set",
        lambda inst, svc, value: store.__setitem__((inst, svc), value),
    )
    return store


def _mock_common(authenticate_return: object) -> MagicMock:
    common = MagicMock()
    if isinstance(authenticate_return, Exception):
        common.authenticate.side_effect = authenticate_return
    else:
        common.authenticate.return_value = authenticate_return
    return common


def _mock_object(generate_return: object) -> MagicMock:
    obj = MagicMock()
    if isinstance(generate_return, Exception):
        obj.execute_kw.side_effect = generate_return
    else:
        obj.execute_kw.return_value = generate_return
    return obj


def test_renew_key_happy_path(
    fake_config: Path,
    fake_keychain: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "the-real-password")
    common = _mock_common(authenticate_return=42)
    obj = _mock_object(generate_return="brand-new-fresh-key")

    def fake_proxy(url: str, **_kw: object) -> MagicMock:
        if "/common" in url:
            return common
        return obj

    monkeypatch.setattr(xmlrpc.client, "ServerProxy", fake_proxy)
    rc = setup_wizard._cmd_renew_key("prod")
    assert rc == 0

    # New key written to keychain.
    assert fake_keychain[("prod", "ODOO_MCP_PROD_API_KEY")] == "brand-new-fresh-key"

    # Authenticate called with the right args.
    common.authenticate.assert_called_once_with(
        "deltix", "timon@deltix.pro", "the-real-password", {}
    )
    # Generate called as the authenticated user, using the password.
    obj.execute_kw.assert_called_once()
    args = obj.execute_kw.call_args.args
    assert args[0] == "deltix"
    assert args[1] == 42  # uid
    assert args[2] == "the-real-password"
    assert args[3] == "res.users.apikeys"
    assert args[4] == "_generate"
    assert args[5][0] == "rpc"
    assert "prod" in args[5][1]  # name includes instance

    out = capsys.readouterr().out
    assert "New API key stored" in out


def test_renew_key_unknown_instance(
    fake_config: Path,
    fake_keychain: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = setup_wizard._cmd_renew_key("ghost")
    assert rc == 1
    out = capsys.readouterr().out
    assert "not configured" in out


def test_renew_key_empty_password_aborts(
    fake_config: Path,
    fake_keychain: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "")
    rc = setup_wizard._cmd_renew_key("prod")
    assert rc == 1
    out = capsys.readouterr().out
    assert "Password cannot be empty" in out
    # Existing key unchanged.
    assert fake_keychain[("prod", "ODOO_MCP_PROD_API_KEY")] == "old-expired-key"


def test_renew_key_wrong_password(
    fake_config: Path,
    fake_keychain: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "wrong")
    common = _mock_common(authenticate_return=xmlrpc.client.Fault(1, "Access denied"))
    monkeypatch.setattr(
        xmlrpc.client,
        "ServerProxy",
        lambda url, **_kw: common,
    )
    rc = setup_wizard._cmd_renew_key("prod")
    assert rc == 1
    out = capsys.readouterr().out
    assert "Odoo rejected" in out
    # Existing key unchanged.
    assert fake_keychain[("prod", "ODOO_MCP_PROD_API_KEY")] == "old-expired-key"


def test_renew_key_zero_uid(
    fake_config: Path,
    fake_keychain: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "pw")
    common = _mock_common(authenticate_return=False)
    monkeypatch.setattr(
        xmlrpc.client,
        "ServerProxy",
        lambda url, **_kw: common,
    )
    rc = setup_wizard._cmd_renew_key("prod")
    assert rc == 1
    out = capsys.readouterr().out
    assert "no uid" in out.lower()


def test_renew_key_missing_username(
    fake_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(setup_wizard, "_keychain_get", lambda *_a: None)
    rc = setup_wizard._cmd_renew_key("prod")
    assert rc == 1
    out = capsys.readouterr().out
    assert "username" in out.lower()


def test_renew_key_generate_fault(
    fake_config: Path,
    fake_keychain: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "pw")
    common = _mock_common(authenticate_return=7)
    obj = _mock_object(generate_return=xmlrpc.client.Fault(2, "duration required"))

    def fake_proxy(url: str, **_kw: object) -> MagicMock:
        return common if "/common" in url else obj

    monkeypatch.setattr(xmlrpc.client, "ServerProxy", fake_proxy)
    rc = setup_wizard._cmd_renew_key("prod")
    assert rc == 1
    out = capsys.readouterr().out
    assert "refused to generate" in out
    assert fake_keychain[("prod", "ODOO_MCP_PROD_API_KEY")] == "old-expired-key"


def test_renew_key_main_dispatch(
    fake_config: Path,
    fake_keychain: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`odoo-mcp renew-key INSTANCE` reaches the right handler."""
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "pw")
    common = _mock_common(authenticate_return=7)
    obj = _mock_object(generate_return="new-key")

    def fake_proxy(url: str, **_kw: object) -> MagicMock:
        return common if "/common" in url else obj

    monkeypatch.setattr(xmlrpc.client, "ServerProxy", fake_proxy)

    from odoo_mcp.__main__ import main

    rc = main_with_argv(main, ["renew-key", "prod"])
    assert rc == 0


def main_with_argv(main_fn: object, argv: list[str]) -> int:
    """Helper: invoke __main__.main() with a synthetic sys.argv."""
    import sys

    real_argv = sys.argv
    try:
        sys.argv = ["odoo-mcp", *argv]
        return main_fn()  # type: ignore[operator,no-any-return]
    finally:
        sys.argv = real_argv


def test_renew_key_missing_arg_returns_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from odoo_mcp.__main__ import main

    rc = main_with_argv(main, ["renew-key"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "Usage" in err
