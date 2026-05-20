"""Tests for ``odoo-mcp renew-key INSTANCE``.

The command is the daily-renewal flow for Odoo Online where non-admin
API keys expire after 1 day. It authenticates with the user's password
once via Odoo's web JSON-RPC endpoint (the XML-RPC layer can't satisfy
the ``@check_identity`` decorator on ``make_key``), creates a fresh
key, stores it, and discards the password. These tests mock the
HTTP/JSON-RPC layer via a fake opener so they run offline.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from odoo_mcp import setup_wizard


class _FakeResponse:
    """Minimal context-manager response object that ``urllib.urlopen`` returns."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._payload


def _install_fake_opener(
    monkeypatch: pytest.MonkeyPatch, *responses: Any
) -> list[urllib.request.Request]:
    """Replace ``urllib.request.build_opener`` so opener.open returns the
    queued *responses* in order. Each response is either a dict
    (serialised as JSON for the body) or an ``Exception`` (raised when
    that call fires). Returns the list that captures every Request the
    code under test sends — used for ordering / payload assertions.
    """
    queue = list(responses)
    captured: list[urllib.request.Request] = []

    def fake_open(req: urllib.request.Request, timeout: float | None = None) -> _FakeResponse:
        captured.append(req)
        if not queue:
            raise AssertionError(
                f"Unexpected extra HTTP call to {req.full_url} — "
                f"queue exhausted after {len(captured)} call(s)."
            )
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeResponse(json.dumps(item).encode("utf-8"))

    opener = MagicMock()
    opener.open.side_effect = fake_open
    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **_kw: opener)
    return captured


def _request_body(req: urllib.request.Request) -> dict[str, Any]:
    """Decode a captured Request's JSON-RPC body."""
    data = req.data
    assert isinstance(data, (bytes, bytearray)), f"unexpected body type: {type(data)!r}"
    return json.loads(data.decode("utf-8"))


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


def test_renew_key_happy_path(
    fake_config: Path,
    fake_keychain: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "the-real-password")
    # First-time renewal: authenticate succeeds (uid 42), cleanup search
    # finds nothing, description record is created (id 99), and make_key
    # returns the Odoo 17+ action shape carrying the key in context.
    captured = _install_fake_opener(
        monkeypatch,
        {"jsonrpc": "2.0", "result": {"uid": 42, "session_id": "sess"}},  # authenticate
        {"jsonrpc": "2.0", "result": []},  # search (no stale)
        {"jsonrpc": "2.0", "result": 99},  # description create
        {
            "jsonrpc": "2.0",
            "result": {
                "type": "ir.actions.act_window",
                "context": {"default_key": "brand-new-fresh-key"},
            },
        },  # make_key
    )

    rc = setup_wizard._cmd_renew_key("prod")
    assert rc == 0
    assert fake_keychain[("prod", "ODOO_MCP_PROD_API_KEY")] == "brand-new-fresh-key"
    assert len(captured) == 4

    # 1) authenticate hits the web JSON-RPC endpoint with db + login + password.
    auth_req = captured[0]
    assert auth_req.full_url.endswith("/web/session/authenticate")
    auth_body = _request_body(auth_req)
    assert auth_body["params"]["db"] == "deltix"
    assert auth_body["params"]["login"] == "timon@deltix.pro"
    assert auth_body["params"]["password"] == "the-real-password"

    # 2) search filters by name AND user_id — the latter is defence in depth
    #    so even if Odoo's ACL on res.users.apikeys regresses, we still
    #    only target the authenticated user's own rows.
    search_req = captured[1]
    assert search_req.full_url.endswith("/web/dataset/call_kw")
    search_body = _request_body(search_req)["params"]
    assert search_body["model"] == "res.users.apikeys"
    assert search_body["method"] == "search"
    domain = search_body["args"][0]
    assert ["user_id", "=", 42] in domain
    assert any(triple[0] == "name" and triple[1] == "=" for triple in domain)

    # 3) create on the description wizard with the desired name.
    create_body = _request_body(captured[2])["params"]
    assert create_body["model"] == "res.users.apikeys.description"
    assert create_body["method"] == "create"
    desc_payload = create_body["args"][0]
    assert "prod" in desc_payload["name"]
    assert " on " in desc_payload["name"]  # hostname suffix in _mcp_key_name

    # 4) make_key on the description record — the whole point of the rewrite
    #    is that this method requires an HTTP session (Odoo 17+
    #    @check_identity), which we now provide.
    make_body = _request_body(captured[3])["params"]
    assert make_body["model"] == "res.users.apikeys.description"
    assert make_body["method"] == "make_key"
    assert make_body["args"] == [[99]]

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
    captured = _install_fake_opener(
        monkeypatch,
        {"jsonrpc": "2.0", "result": {"uid": 42}},  # authenticate
        {"jsonrpc": "2.0", "result": [10, 11, 12]},  # search → stale ids
        {"jsonrpc": "2.0", "result": True},  # unlink
        {"jsonrpc": "2.0", "result": 77},  # description create
        {
            "jsonrpc": "2.0",
            "result": {"type": "ir.actions.act_window", "context": {"default_key": "fresh-key"}},
        },
    )
    rc = setup_wizard._cmd_renew_key("prod")
    assert rc == 0
    assert len(captured) == 5

    # Sequence: authenticate, search, unlink, create, make_key.
    auth, search, unlink, create, make = captured
    assert auth.full_url.endswith("/web/session/authenticate")
    unlink_body = _request_body(unlink)["params"]
    assert unlink_body["model"] == "res.users.apikeys"
    assert unlink_body["method"] == "unlink"
    assert unlink_body["args"] == [[10, 11, 12]]
    assert _request_body(search)["params"]["method"] == "search"
    assert _request_body(create)["params"]["method"] == "create"
    assert _request_body(make)["params"]["method"] == "make_key"

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
    # Search returns an Odoo application error (not an HTTP error). The
    # cleanup path catches it and logs a warning; the description wizard
    # then still runs and make_key succeeds.
    _install_fake_opener(
        monkeypatch,
        {"jsonrpc": "2.0", "result": {"uid": 42}},
        {"jsonrpc": "2.0", "error": {"data": {"message": "Access denied to apikeys"}}},
        {"jsonrpc": "2.0", "result": 55},
        {
            "jsonrpc": "2.0",
            "result": {
                "type": "ir.actions.act_window",
                "context": {"default_key": "still-got-fresh-key"},
            },
        },
    )

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
    _install_fake_opener(
        monkeypatch,
        {"jsonrpc": "2.0", "error": {"data": {"message": "Access denied"}}},
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
    """Odoo returns ``{"uid": false}`` on a silent auth failure (e.g. wrong
    db). Treat that the same as a hard rejection — no uid means no
    further calls are safe."""
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "pw")
    _install_fake_opener(
        monkeypatch,
        {"jsonrpc": "2.0", "result": {"uid": False}},
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
    # Cleanup search OK (no stale), then the description create faults —
    # exercises the user-readable fault formatter on the wizard side.
    _install_fake_opener(
        monkeypatch,
        {"jsonrpc": "2.0", "result": {"uid": 7}},
        {"jsonrpc": "2.0", "result": []},
        {
            "jsonrpc": "2.0",
            "error": {"data": {"message": "Access denied to res.users.apikeys.description"}},
        },
    )
    rc = setup_wizard._cmd_renew_key("prod")
    assert rc == 1
    out = capsys.readouterr().out
    assert "refused to generate" in out
    assert fake_keychain[("prod", "ODOO_MCP_PROD_API_KEY")] == "old-expired-key"


def test_renew_key_http_only_fault_surfaces_helpful_message(
    fake_config: Path,
    fake_keychain: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The exact failure Timon hit on Odoo Online v0.17.6 before this
    rewrite: ``@check_identity`` rejects because no HTTP context. If
    that somehow surfaces again (proxy stripping cookies, mid-session
    expiry, etc.), the user must see actionable instructions — not a
    raw stack trace."""
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "pw")
    _install_fake_opener(
        monkeypatch,
        {"jsonrpc": "2.0", "result": {"uid": 7}},
        {"jsonrpc": "2.0", "result": []},
        {"jsonrpc": "2.0", "result": 1},
        {
            "jsonrpc": "2.0",
            "error": {"data": {"message": "Deze methode is alleen toegankelijk via HTTP"}},
        },
    )
    rc = setup_wizard._cmd_renew_key("prod")
    assert rc == 1
    out = capsys.readouterr().out
    # The formatter must mention what to do, not just dump the raw error.
    assert "Account Security" in out
    assert "option 1" in out


def test_renew_key_main_dispatch(
    fake_config: Path,
    fake_keychain: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`odoo-mcp renew-key INSTANCE` reaches the right handler."""
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "pw")
    _install_fake_opener(
        monkeypatch,
        {"jsonrpc": "2.0", "result": {"uid": 7}},
        {"jsonrpc": "2.0", "result": []},
        {"jsonrpc": "2.0", "result": 11},
        {
            "jsonrpc": "2.0",
            "result": {"type": "ir.actions.act_window", "context": {"default_key": "new-key"}},
        },
    )

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
