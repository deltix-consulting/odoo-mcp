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
    obj = MagicMock()
    # First-time renewal flow: cleanup search returns no stale keys,
    # the description wizard is created (id 99), and make_key returns
    # an action dict whose context.default_key holds the new key —
    # this is the shape Odoo 17+ returns from the Account-Security
    # "New API Key" wizard.
    make_key_action = {
        "type": "ir.actions.act_window",
        "context": {"default_key": "brand-new-fresh-key"},
    }
    obj.execute_kw.side_effect = [[], 99, make_key_action]

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
    # Three execute_kw calls: search (cleanup), description create, make_key.
    assert obj.execute_kw.call_count == 3
    search_args, create_args, make_args = obj.execute_kw.call_args_list
    # Search: filtered by name + user_id, on the user's own apikeys.
    assert search_args.args[3] == "res.users.apikeys"
    assert search_args.args[4] == "search"
    domain = search_args.args[5][0]
    assert ("user_id", "=", 42) in domain
    assert any(triple[0] == "name" and triple[1] == "=" for triple in domain)
    # Create: the description wizard record carries the desired name.
    assert create_args.args[3] == "res.users.apikeys.description"
    assert create_args.args[4] == "create"
    desc_payload = create_args.args[5][0]
    assert isinstance(desc_payload, dict)
    assert "prod" in desc_payload["name"]
    assert " on " in desc_payload["name"]  # hostname suffix
    # make_key: called as the authenticated user with the password on the
    # description record we just created. The non-underscore method name
    # is the whole point — Odoo blocks RPC calls to _generate.
    assert make_args.args[0] == "deltix"
    assert make_args.args[1] == 42  # uid
    assert make_args.args[2] == "the-real-password"
    assert make_args.args[3] == "res.users.apikeys.description"
    assert make_args.args[4] == "make_key"
    assert make_args.args[5] == [[99]]

    out = capsys.readouterr().out
    assert "New API key stored" in out


def test_renew_key_cleans_up_stale_keys(
    fake_config: Path,
    fake_keychain: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When previous renewals left stale rows, they get unlinked before generation."""
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "pw")
    common = _mock_common(authenticate_return=42)
    obj = MagicMock()
    # search → 3 stale ids, unlink → True, description create → 77,
    # make_key → action dict carrying the new key.
    obj.execute_kw.side_effect = [
        [10, 11, 12],
        True,
        77,
        {"type": "ir.actions.act_window", "context": {"default_key": "fresh-key"}},
    ]

    def fake_proxy(url: str, **_kw: object) -> MagicMock:
        return common if "/common" in url else obj

    monkeypatch.setattr(xmlrpc.client, "ServerProxy", fake_proxy)
    rc = setup_wizard._cmd_renew_key("prod")
    assert rc == 0
    assert obj.execute_kw.call_count == 4

    # Unlink called with the ids the search returned; create + make_key
    # follow on the description wizard.
    _search, unlink, create, make = obj.execute_kw.call_args_list
    assert unlink.args[3] == "res.users.apikeys"
    assert unlink.args[4] == "unlink"
    assert unlink.args[5] == [[10, 11, 12]]
    assert create.args[3] == "res.users.apikeys.description"
    assert create.args[4] == "create"
    assert make.args[3] == "res.users.apikeys.description"
    assert make.args[4] == "make_key"

    out = capsys.readouterr().out
    assert "Removed 3 stale API key(s)" in out
    assert "New API key stored" in out


def test_renew_key_cleanup_failure_is_best_effort(
    fake_config: Path,
    fake_keychain: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """If the cleanup search faults, the renewal still succeeds with a warning."""
    import io
    import logging

    monkeypatch.setattr("getpass.getpass", lambda _prompt: "pw")
    common = _mock_common(authenticate_return=42)
    obj = MagicMock()
    # Search faults; the description wizard still runs and make_key
    # returns the new key in the action context.
    obj.execute_kw.side_effect = [
        xmlrpc.client.Fault(1, "Access denied to apikeys"),
        55,
        {"type": "ir.actions.act_window", "context": {"default_key": "still-got-fresh-key"}},
    ]

    def fake_proxy(url: str, **_kw: object) -> MagicMock:
        return common if "/common" in url else obj

    monkeypatch.setattr(xmlrpc.client, "ServerProxy", fake_proxy)

    # Attach our own handler AND pin the logger level — caplog is flaky
    # across the full suite because other tests reconfigure module-level
    # logging (e.g. test_logging.py raises the level to ERROR, which
    # suppresses our warning at the logger before it ever reaches the
    # handler). Reading from a stream we own AND restoring the prior
    # level avoids both failure modes.
    target_logger = logging.getLogger("odoo_mcp.setup_wizard")
    captured = io.StringIO()
    handler = logging.StreamHandler(captured)
    handler.setLevel(logging.WARNING)
    prior_level = target_logger.level
    target_logger.setLevel(logging.WARNING)
    target_logger.addHandler(handler)
    try:
        rc = setup_wizard._cmd_renew_key("prod")
    finally:
        target_logger.removeHandler(handler)
        target_logger.setLevel(prior_level)

    assert rc == 0
    assert fake_keychain[("prod", "ODOO_MCP_PROD_API_KEY")] == "still-got-fresh-key"
    assert "clean up old API keys" in captured.getvalue()
    out = capsys.readouterr().out
    assert "New API key stored" in out
    # No "Removed N" line — cleanup didn't actually delete anything.
    assert "Removed" not in out


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
    obj = MagicMock()
    # Cleanup search OK (no stale), then the description create faults —
    # exercise the user-readable fault formatter on make_key's side of
    # the wizard call.
    obj.execute_kw.side_effect = [
        [],
        xmlrpc.client.Fault(2, "Access denied to res.users.apikeys.description"),
    ]

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
    obj = MagicMock()
    # Same three-call shape as the happy path: search, create, make_key.
    obj.execute_kw.side_effect = [
        [],
        11,
        {"type": "ir.actions.act_window", "context": {"default_key": "new-key"}},
    ]

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
