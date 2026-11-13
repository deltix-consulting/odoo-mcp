"""``authenticate`` against a URL that answers, but not as Odoo.

``authenticate`` is the first request of every session, of ``odoo-mcp
doctor`` and of the setup wizard. It mapped socket / TLS / timeout failures
and nothing else, so the replies a wrong URL actually produces escaped as
raw stdlib exceptions:

* ``http://`` on a server that only serves ``https://`` -> ``301`` ->
  ``xmlrpc.client.ProtocolError`` (the stdlib client follows no redirects);
* a URL with a path, a proxy in front of a restarting Odoo -> ``404`` /
  ``502`` -> ``ProtocolError``;
* an SSO portal or another site -> an HTML page with a 200 -> ``ExpatError``.

These tests run a real HTTP server on localhost so the stdlib transport is
exercised end to end, not a fake of it.
"""

from __future__ import annotations

import http.server
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from odoo_mcp import doctor
from odoo_mcp.client import OdooClient, _describe_bad_reply
from odoo_mcp.config import InstanceConfig
from odoo_mcp.credentials import Credentials
from odoo_mcp.errors import OdooTransportError
from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD

_HTML = b"<!DOCTYPE html><html><head><title>Sign in</title></head><body><p>hi<br></body></html>"


class _Server(http.server.HTTPServer):
    mode: str = "301"
    location: str = "https://acme.odoo.com/xmlrpc/2/common?session=abc123"


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:  # silence stderr
        return

    def do_POST(self) -> None:  # noqa: N802 — stdlib naming
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        server = self.server
        assert isinstance(server, _Server)
        if server.mode == "html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(_HTML)))
            self.end_headers()
            self.wfile.write(_HTML)
            return
        self.send_response(int(server.mode))
        if server.mode.startswith("3"):
            self.send_header("Location", server.location)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Server]:
    # A proxy configured in the developer's environment must not sit between
    # the client and the loopback server.
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "*")
    srv = _Server(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def _client(srv: _Server) -> OdooClient:
    cfg = InstanceConfig(
        name="dev",
        url=f"http://127.0.0.1:{srv.server_port}",
        database="db",
        credentials_env_prefix="ODOO_MCP_DEV",
        production=False,
        timeout_seconds=5,
        max_records_default=50,
        max_records_hard_cap=500,
        rate_limit_per_minute=300,
        allow_self_signed=False,
        allowed_models=frozenset({ALLOWLIST_WILDCARD}),
    )
    return OdooClient(
        cfg, credentials=Credentials(instance_name="dev", username="u", _api_key="k" * 10)
    )


def _auth_error(srv: _Server, mode: str) -> OdooTransportError:
    srv.mode = mode
    with pytest.raises(OdooTransportError) as info:
        _client(srv).authenticate()
    return info.value


# ---------------------------------------------------------------------------
# 1. The three replies map to odoo_transport
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["301", "302", "307", "308", "403", "404", "502", "503", "html"])
def test_non_xmlrpc_reply_is_a_transport_error(server: _Server, mode: str) -> None:
    err = _auth_error(server, mode)
    assert err.code == "odoo_transport"
    assert "authenticating against 'dev'" in err.user_message
    assert "doctor" in err.hint


def test_redirect_names_the_target_host_and_the_fix(server: _Server) -> None:
    msg = _auth_error(server, "301").user_message
    assert "HTTP 301" in msg
    assert "https://acme.odoo.com" in msg
    assert "does not follow redirects" in msg


def test_redirect_never_echoes_the_location_path_or_query(server: _Server) -> None:
    server.location = "https://sso.example.com/login?token=SECRETTOKEN&next=/xmlrpc/2/common"
    msg = _auth_error(server, "302").user_message
    assert "https://sso.example.com" in msg
    assert "SECRETTOKEN" not in msg
    assert "/login" not in msg


def test_redirect_drops_userinfo_from_the_location(server: _Server) -> None:
    server.location = "https://bob:hunter2pass@odoo.example.com:8443/xmlrpc/2/common"
    msg = _auth_error(server, "307").user_message
    assert "https://odoo.example.com:8443" in msg
    assert "hunter2pass" not in msg
    assert "bob" not in msg


@pytest.mark.parametrize("location", ["", "/web/login", "not a url", "https://[bad"])
def test_redirect_with_an_unusable_location_still_explains(server: _Server, location: str) -> None:
    server.location = location
    msg = _auth_error(server, "301").user_message
    assert "the server redirects." in msg
    assert "does not follow redirects" in msg


def test_404_says_the_url_must_be_the_base_address(server: _Server) -> None:
    msg = _auth_error(server, "404").user_message
    assert "HTTP 404" in msg
    assert "base address" in msg


def test_html_page_says_it_is_not_odoo(server: _Server) -> None:
    msg = _auth_error(server, "html").user_message
    assert "not XML-RPC" in msg
    # The parser position of someone else's HTML helps nobody.
    assert "line 1" not in msg


def test_message_does_not_echo_the_request_netloc(server: _Server) -> None:
    """``str(ProtocolError)`` embeds host + handler; the message must not."""
    for mode in ("301", "404", "502"):
        msg = _auth_error(server, mode).user_message
        assert f"127.0.0.1:{server.server_port}" not in msg
        assert "/xmlrpc/2/common" not in msg.replace("at /xmlrpc/2/common", "")


def test_uid_stays_unset_after_a_bad_reply(server: _Server) -> None:
    server.mode = "502"
    client = _client(server)
    with pytest.raises(OdooTransportError):
        client.authenticate()
    assert client._uid is None


# ---------------------------------------------------------------------------
# 2. The helper on its own
# ---------------------------------------------------------------------------


def test_describe_falls_back_to_type_and_text_for_other_http_errors() -> None:
    import http.client

    assert _describe_bad_reply(http.client.IncompleteRead(b"ab", 10)).startswith("IncompleteRead")


# ---------------------------------------------------------------------------
# 3. doctor — the command every transport hint points to
# ---------------------------------------------------------------------------


def _doctor_config(tmp_path: Path, port: int) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[defaults]\n"
        f'audit_log = "{tmp_path / "audit.jsonl"}"\n'
        'fields_cache_path = ""\n'
        "\n"
        "[instances.dev]\n"
        f'url = "http://127.0.0.1:{port}"\n'
        'database = "db"\n'
        'credentials_env_prefix = "ODOO_MCP_DEV"\n'
        "production = false\n"
    )
    os.chmod(cfg, 0o600)
    return cfg


@pytest.mark.parametrize("mode", ["301", "502", "html"])
def test_doctor_reports_a_bad_reply_instead_of_crashing(
    server: _Server,
    mode: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from odoo_mcp import _credstore, setup_wizard

    server.mode = mode
    monkeypatch.setattr(setup_wizard, "load_credentials_into_os", lambda: 0)
    monkeypatch.setattr(_credstore, "get_secret_set_at", lambda _i, _s: None)
    monkeypatch.setenv("ODOO_MCP_DEV_USERNAME", "u")
    monkeypatch.setenv("ODOO_MCP_DEV_API_KEY", "k" * 10)

    rc = doctor.run_doctor(_doctor_config(tmp_path, server.server_port))

    out = capsys.readouterr().out
    assert rc == 1
    assert "authenticate" in out
    assert "Bad response authenticating" in out
