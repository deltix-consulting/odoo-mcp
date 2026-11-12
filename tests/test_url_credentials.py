"""An instance URL that carries HTTP Basic credentials is a secret.

``url = "https://user:pass@odoo.example.com"`` is how an Odoo behind a
basic-auth reverse proxy is reached, and the XML-RPC transport honours it
(``Transport.get_host_info`` turns the userinfo into an ``Authorization``
header). The same text must not reach the model, the audit log or the
``status`` / ``config show`` output:

* ``odoo_list_instances`` echoed ``url`` verbatim.
* ``xmlrpc.client.ProtocolError`` puts ``user:pass@host/xmlrpc/2/...`` in its
  message on any non-200 reply (a 401 after the proxy password rotated, a
  502 while Odoo restarts), and that message became the tool error.

The transport test below drives the real stdlib request path with a fake
connection, so it also pins the stdlib behaviour the fix exists for.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from odoo_mcp import errors
from odoo_mcp.audit import AuditLog
from odoo_mcp.client import OdooClient
from odoo_mcp.config import AppConfig, load_config, split_url_userinfo
from odoo_mcp.config_cli import _render_show
from odoo_mcp.credentials import Credentials
from odoo_mcp.dispatcher import Dispatcher, InstanceRuntime, OdooMcpApp
from odoo_mcp.security.limits import RateLimiter
from odoo_mcp.security.prod_guard import ProdGuard

_USER = "proxyuser"
_PASSWORD = "S3cr3t-proxy-pw"  # noqa: S105 — test fixture
_HOST = "staging.example.com"

_CONFIG = f"""
[instances.dev]
url = "https://{_USER}:{_PASSWORD}@{_HOST}"
database = "dev_db"
credentials_env_prefix = "ODOO_MCP_DEV"
production = false
"""


@pytest.fixture(autouse=True)
def _isolated_secret_registry() -> Iterator[None]:
    snapshot = list(errors._SECRETS.keys())
    yield
    errors._SECRETS.clear()
    for secret in snapshot:
        errors._SECRETS[secret] = None
    errors._SECRETS_PATTERN = None


def _load(tmp_path: Path, body: str = _CONFIG) -> AppConfig:
    path = tmp_path / "config.toml"
    path.write_text(body)
    if os.name == "posix":
        path.chmod(0o600)
    return load_config(path)


class _Response:
    status = 401
    reason = "Unauthorized"

    def getheader(self, _name: str, default: str | None = None) -> str | None:
        return default

    def getheaders(self) -> list[tuple[str, str]]:
        return []

    def read(self) -> bytes:
        return b""


class _RefusingConnection:
    """Stands in for the HTTPS connection; every request gets a 401."""

    def putrequest(self, *_a: Any, **_kw: Any) -> None:
        return None

    def putheader(self, *_a: Any, **_kw: Any) -> None:
        return None

    def endheaders(self, *_a: Any, **_kw: Any) -> None:
        return None

    def getresponse(self) -> _Response:
        return _Response()

    def close(self) -> None:
        return None


def _app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> OdooMcpApp:
    app_cfg = _load(tmp_path)
    cfg = app_cfg.instances["dev"]
    creds = Credentials(instance_name=cfg.name, username="u", _api_key="k" * 10)
    client = OdooClient(cfg, credentials=creds)
    for proxy_transport in (client._object_transport, client._common("transport")):
        monkeypatch.setattr(proxy_transport, "make_connection", lambda _host: _RefusingConnection())
    rl = RateLimiter()
    rl.configure(cfg.name, cfg.rate_limit_per_minute)
    return OdooMcpApp(
        config=AppConfig(
            path=app_cfg.path,
            defaults=app_cfg.defaults,
            instances=app_cfg.instances,
            audit_log_path=tmp_path / "audit.jsonl",
        ),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        prod_guard=ProdGuard(),
        rate_limiter=rl,
        instances={cfg.name: InstanceRuntime(config=cfg, client=client)},
    )


def _call_text(disp: Dispatcher, tool: str, args: dict[str, Any]) -> str:
    contents = asyncio.run(disp.call(tool, args))
    return str(contents[0].text)


# ---------------------------------------------------------------------------
# split_url_userinfo
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "display", "userinfo"),
    [
        ("https://odoo.example.com", "https://odoo.example.com", None),
        ("https://odoo.example.com:8069/odoo", "https://odoo.example.com:8069/odoo", None),
        ("https://u:p@odoo.example.com", "https://odoo.example.com", "u:p"),
        ("https://u:p@odoo.example.com:8443/x", "https://odoo.example.com:8443/x", "u:p"),
        ("http://token@localhost:8069", "http://localhost:8069", "token"),
        # An unescaped "@" inside the password: the host is what follows the LAST "@".
        ("https://u:p@ss@odoo.example.com", "https://odoo.example.com", "u:p@ss"),
        ("https://u:p%40ss@odoo.example.com", "https://odoo.example.com", "u:p%40ss"),
    ],
)
def test_split_url_userinfo(url: str, display: str, userinfo: str | None) -> None:
    assert split_url_userinfo(url) == (display, userinfo)


# ---------------------------------------------------------------------------
# Config load
# ---------------------------------------------------------------------------


def test_url_keeps_credentials_for_the_transport_and_display_url_drops_them(
    tmp_path: Path,
) -> None:
    inst = _load(tmp_path).instances["dev"]
    # The transport needs them to authenticate against the proxy.
    assert inst.url == f"https://{_USER}:{_PASSWORD}@{_HOST}"
    assert inst.display_url == f"https://{_HOST}"


def test_display_url_equals_url_without_credentials(tmp_path: Path) -> None:
    body = _CONFIG.replace(f"{_USER}:{_PASSWORD}@", "")
    inst = _load(tmp_path, body).instances["dev"]
    assert inst.display_url == inst.url == f"https://{_HOST}"


def test_loading_the_config_registers_the_url_credentials_as_secrets(tmp_path: Path) -> None:
    leaked = f"<ProtocolError for {_USER}:{_PASSWORD}@{_HOST}/xmlrpc/2/object: 502 Bad Gateway>"
    assert errors.redact(leaked) == leaked  # nothing registered yet
    _load(tmp_path)
    scrubbed = errors.redact(leaked)
    assert _PASSWORD not in scrubbed
    assert _HOST in scrubbed  # the useful half of the message survives
    assert _PASSWORD not in errors.redact(f"password {_PASSWORD} on its own")


def test_short_password_is_scrubbed_as_part_of_the_pair_only(tmp_path: Path) -> None:
    """A 4-letter password must not turn every "odoo" in an error into <redacted>."""
    _load(tmp_path, _CONFIG.replace(_PASSWORD, "odoo"))
    assert "odoo" not in errors.redact(f"for {_USER}:odoo@{_HOST}/xmlrpc").replace(_HOST, "")
    assert errors.redact("Odoo fault on odoo.thing") == "Odoo fault on odoo.thing"


def test_percent_encoded_password_is_scrubbed_in_both_forms(tmp_path: Path) -> None:
    _load(tmp_path, _CONFIG.replace(_PASSWORD, "p%40ssw0rd-long"))
    assert "p%40ssw0rd-long" not in errors.redact("saw p%40ssw0rd-long")
    assert "p@ssw0rd-long" not in errors.redact("saw p@ssw0rd-long")


# ---------------------------------------------------------------------------
# What the model, the audit log and the operator see
# ---------------------------------------------------------------------------


def test_list_instances_does_not_return_the_url_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = _call_text(Dispatcher(_app(tmp_path, monkeypatch)), "odoo_list_instances", {})
    assert _PASSWORD not in text
    assert _USER not in text
    [summary] = json.loads(text)["instances"]
    assert summary["url"] == f"https://{_HOST}"


def test_proxy_refusal_does_not_leak_the_url_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 401 from the proxy: the stdlib names ``user:pass@host`` in its error."""
    app = _app(tmp_path, monkeypatch)
    text = _call_text(
        Dispatcher(app),
        "odoo_search_read",
        {"instance": "dev", "model": "res.partner", "fields": ["id"]},
    )
    payload = json.loads(text)
    assert payload["ok"] is False
    assert "401" in payload["error"]  # the real failure is still reported
    assert _HOST in payload["error"]
    assert _PASSWORD not in text
    audit_text = (tmp_path / "audit.jsonl").read_text()
    assert audit_text  # the refusal was audited
    assert _PASSWORD not in audit_text


def test_config_show_does_not_print_the_url_credentials(tmp_path: Path) -> None:
    rendered = "\n".join(_render_show(_load(tmp_path), check_keychain=False))
    assert _PASSWORD not in rendered
    assert f"https://{_HOST}" in rendered
