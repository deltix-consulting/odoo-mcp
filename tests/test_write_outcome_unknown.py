"""A write that fails AFTER the request reached Odoo must say so.

The transport already refuses to re-send a non-idempotent call after a
dropped connection (``_ConnectionRecyclingMixin``), because the server may
have committed the ``create`` before it died. But the error that came back
to the agent read "Timeout ... Check that the Odoo URL is reachable" —
which is an invitation to re-issue the call, and the agent is the next
retry loop. pantalytics/odoo-mcp-pro #139 is the production shape of this:
``message_post`` committed, sent the email, then failed to marshal the
reply; the tool reported failure, the caller retried, the customer got the
mail twice.

These tests pin the three halves of the fix:

1. ``OdooClient._execute`` marks a failure ``outcome_unknown`` only when the
   call is non-idempotent AND the request was fully sent (socket-level
   failures) or the fault is the post-commit marshalling one.
2. Reply-shaped failures that are not ``OSError`` (proxy 504, truncated
   body, HTML error page) map to ``OdooTransportError`` instead of escaping
   as ``internal_error``.
3. The dispatcher surfaces the flag as ``outcome: "unknown"`` in the tool
   response and in the audit row.
"""

from __future__ import annotations

import asyncio
import http.client
import io
import json
import xmlrpc.client
from pathlib import Path
from typing import Any

import pytest

from odoo_mcp.audit import AuditLog
from odoo_mcp.client import OdooClient, _TimeoutTransport
from odoo_mcp.config import AppConfig, Defaults, InstanceConfig
from odoo_mcp.credentials import Credentials
from odoo_mcp.dispatcher import Dispatcher, InstanceRuntime, OdooMcpApp
from odoo_mcp.errors import (
    OUTCOME_UNKNOWN_HINT,
    OdooMcpError,
    OdooRemoteError,
    OdooTransportError,
)
from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD
from odoo_mcp.security.limits import RateLimiter
from odoo_mcp.security.prod_guard import ProdGuard

# ---------------------------------------------------------------------------
# Fake HTTP connection driven through the REAL xmlrpc transport stack
# ---------------------------------------------------------------------------


class _Reply:
    """Minimal ``HTTPResponse`` stand-in: a 200 with a fixed body."""

    status = 200

    def __init__(self, body: bytes) -> None:
        self._buf = io.BytesIO(body)

    def getheader(self, name: str, default: Any = None) -> Any:
        return default

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)


class _FakeConn:
    """Accepts the request; ``getresponse`` raises or returns ``reply``."""

    def __init__(self, *, fail: BaseException | None = None, reply: bytes | None = None) -> None:
        self._fail = fail
        self._reply = reply
        self.sock = None
        self.body_sent = False

    def putrequest(self, *args: Any, **kwargs: Any) -> None:
        return None

    def putheader(self, *args: Any, **kwargs: Any) -> None:
        return None

    def endheaders(self, message_body: bytes | None = None) -> None:
        self.body_sent = True

    def getresponse(self) -> _Reply:
        if self._fail is not None:
            raise self._fail
        assert self._reply is not None
        return _Reply(self._reply)

    def close(self) -> None:
        return None


def _cfg(*, production: bool = False) -> InstanceConfig:
    return InstanceConfig(
        name="dev",
        url="http://odoo.example.com",
        database="db",
        credentials_env_prefix="ODOO_MCP_DEV",
        production=production,
        timeout_seconds=30,
        max_records_default=50,
        max_records_hard_cap=500,
        rate_limit_per_minute=300,
        allow_self_signed=False,
        allowed_models=frozenset({ALLOWLIST_WILDCARD}),
    )


def _client(conn: _FakeConn | None = None, *, refuse: bool = False) -> OdooClient:
    """A real ``OdooClient`` whose object transport dials ``conn``.

    ``refuse=True`` makes the dial itself fail (connection refused), i.e.
    the request body is never sent.
    """
    client = OdooClient(
        _cfg(), credentials=Credentials(instance_name="dev", username="u", _api_key="k" * 10)
    )
    client._uid = 7

    def make_connection(host: Any) -> Any:
        if refuse:
            raise ConnectionRefusedError(111, "Connection refused")
        return conn

    client._object_transport.make_connection = make_connection  # type: ignore[method-assign]
    return client


def _fault(text: str) -> bytes:
    return xmlrpc.client.dumps(xmlrpc.client.Fault(1, text), methodresponse=True).encode()


def _raises(client: OdooClient, method: str) -> OdooMcpError:
    args: list[Any] = [[{"name": "x"}]] if method == "create" else [[]]
    with pytest.raises(OdooMcpError) as info:
        client._execute("res.partner", method, args, {})
    return info.value


# ---------------------------------------------------------------------------
# 1. Which failures are flagged
# ---------------------------------------------------------------------------


def test_create_timeout_after_send_is_outcome_unknown() -> None:
    """Odoo had the whole request and never answered: it may have committed."""
    err = _raises(_client(_FakeConn(fail=TimeoutError("timed out"))), "create")
    assert isinstance(err, OdooTransportError)
    assert err.outcome_unknown is True
    assert err.hint is not None and err.hint.startswith(OUTCOME_UNKNOWN_HINT)
    # The original diagnostic is still there — appended, not replaced.
    assert "odoo-mcp doctor" in err.hint


def test_create_refused_before_send_is_not_flagged() -> None:
    """Connection refused: nothing reached Odoo, retrying is safe."""
    err = _raises(_client(refuse=True), "create")
    assert isinstance(err, OdooTransportError)
    assert err.outcome_unknown is False
    assert err.hint is not None and OUTCOME_UNKNOWN_HINT not in err.hint


def test_read_timeout_after_send_is_not_flagged() -> None:
    """Idempotent reads never carry the flag, sent or not."""
    err = _raises(_client(_FakeConn(fail=TimeoutError("timed out"))), "search_read")
    assert isinstance(err, OdooTransportError)
    assert err.outcome_unknown is False


def test_marshal_fault_after_commit_is_outcome_unknown() -> None:
    """The ``cannot marshal`` fault is raised after the cursor committed."""
    body = _fault("Traceback ...\nTypeError: cannot marshal None unless allow_none is enabled")
    err = _raises(_client(_FakeConn(reply=body)), "message_post")
    assert isinstance(err, OdooRemoteError)
    assert err.outcome_unknown is True
    assert err.hint == OUTCOME_UNKNOWN_HINT


def test_validation_fault_rolls_back_and_is_not_flagged() -> None:
    """A normal Odoo fault means the transaction rolled back — no flag."""
    body = _fault("odoo.exceptions.ValidationError: The email is already taken")
    err = _raises(_client(_FakeConn(reply=body)), "create")
    assert isinstance(err, OdooRemoteError)
    assert err.outcome_unknown is False
    assert err.hint is None


def test_marshal_fault_on_a_read_is_not_flagged() -> None:
    body = _fault("TypeError: cannot marshal <class 'x'> objects")
    err = _raises(_client(_FakeConn(reply=body)), "search_read")
    assert err.outcome_unknown is False


# ---------------------------------------------------------------------------
# 2. Reply-shaped failures used to escape as internal_error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fail",
    [
        xmlrpc.client.ProtocolError(
            "odoo.example.com/xmlrpc/2/object", 504, "Gateway Time-out", {}
        ),
        http.client.IncompleteRead(b""),
    ],
    ids=["proxy_504", "truncated_body"],
)
def test_reply_failures_map_to_transport_error_and_are_flagged(fail: BaseException) -> None:
    err = _raises(_client(_FakeConn(fail=fail)), "create")
    assert isinstance(err, OdooTransportError)
    assert err.code == "odoo_transport"
    assert err.outcome_unknown is True
    assert type(fail).__name__ in err.user_message


def test_html_error_page_maps_to_transport_error_and_is_flagged() -> None:
    """A proxy serving its HTML error page with a 200 is not XML-RPC."""
    err = _raises(_client(_FakeConn(reply=b"<html><body>502 Bad Gateway</body></html>")), "create")
    assert isinstance(err, OdooTransportError)
    assert err.outcome_unknown is True


def test_reply_failure_on_a_read_is_transport_error_without_flag() -> None:
    fail = xmlrpc.client.ProtocolError("odoo.example.com/xmlrpc/2/object", 502, "Bad Gateway", {})
    err = _raises(_client(_FakeConn(fail=fail)), "search_read")
    assert isinstance(err, OdooTransportError)
    assert err.outcome_unknown is False


# ---------------------------------------------------------------------------
# message_post's own post-commit raise
# ---------------------------------------------------------------------------


def test_message_post_unexpected_return_is_outcome_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Odoo returned normally, so the message IS posted — the id is just unreadable."""
    client = _client(_FakeConn())
    monkeypatch.setattr(client, "_execute", lambda *a, **k: False)
    with pytest.raises(OdooRemoteError) as info:
        client.message_post(
            "res.partner", 1, "hi", subject=None, partner_ids=[], message_type="comment"
        )
    assert info.value.outcome_unknown is True
    assert "the message was posted" in info.value.user_message


# ---------------------------------------------------------------------------
# Transport: the sent-flag itself
# ---------------------------------------------------------------------------


def test_request_was_sent_tracks_the_body_going_out() -> None:
    transport = _TimeoutTransport(timeout=5.0)
    conn = _FakeConn(fail=TimeoutError("timed out"))
    transport.make_connection = lambda host: conn  # type: ignore[method-assign]
    assert transport.request_was_sent is False
    with pytest.raises(TimeoutError), transport.suppress_retry():
        transport.request("odoo.example.com", "/xmlrpc/2/object", b"<xml/>")
    assert conn.body_sent is True
    assert transport.request_was_sent is True


def test_request_was_sent_resets_on_the_next_request() -> None:
    """The flag describes the CURRENT request, never a previous one."""
    transport = _TimeoutTransport(timeout=5.0)
    transport.make_connection = lambda host: _FakeConn(fail=TimeoutError("t"))  # type: ignore[method-assign]
    with pytest.raises(TimeoutError), transport.suppress_retry():
        transport.request("odoo.example.com", "/xmlrpc/2/object", b"<xml/>")
    assert transport.request_was_sent is True

    def refuse(host: Any) -> Any:
        raise ConnectionRefusedError(111, "Connection refused")

    transport.make_connection = refuse  # type: ignore[method-assign]
    with pytest.raises(ConnectionRefusedError), transport.suppress_retry():
        transport.request("odoo.example.com", "/xmlrpc/2/object", b"<xml/>")
    assert transport.request_was_sent is False


def test_request_was_sent_stays_false_when_the_body_send_fails() -> None:
    """EPIPE mid-body: Odoo never saw a complete request."""
    transport = _TimeoutTransport(timeout=5.0)
    conn = _FakeConn()

    def endheaders(message_body: bytes | None = None) -> None:
        raise BrokenPipeError(32, "Broken pipe")

    conn.endheaders = endheaders  # type: ignore[method-assign]
    transport.make_connection = lambda host: conn  # type: ignore[method-assign]
    with pytest.raises(BrokenPipeError), transport.suppress_retry():
        transport.request("odoo.example.com", "/xmlrpc/2/object", b"<xml/>")
    assert transport.request_was_sent is False


# ---------------------------------------------------------------------------
# 3. Dispatcher: response payload + audit row
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, error: OdooMcpError) -> None:
        self.is_admin: bool | None = False
        self.admin_reason: str | None = None
        self.username = "u"
        self.uid = 1
        self._error = error

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        return {"id": {"type": "integer"}, "name": {"type": "char"}}

    def create(self, model: str, values: dict[str, Any]) -> int:
        raise self._error


def _build(tmp_path: Path, error: OdooMcpError) -> Dispatcher:
    cfg = _cfg()
    creds = Credentials(instance_name=cfg.name, username="u", _api_key="k" * 10)
    real = OdooClient(cfg, credentials=creds)
    app_cfg = AppConfig(
        path=tmp_path / "config.toml",
        defaults=Defaults(),
        instances={cfg.name: cfg},
        audit_log_path=tmp_path / "audit.jsonl",
    )
    rl = RateLimiter()
    rl.configure(cfg.name, cfg.rate_limit_per_minute)
    rt = InstanceRuntime(config=cfg, client=real)
    rt.client = _FakeClient(error)  # type: ignore[assignment]
    app = OdooMcpApp(
        config=app_cfg,
        audit=AuditLog(app_cfg.audit_log_path),
        prod_guard=ProdGuard(),
        rate_limiter=rl,
        instances={cfg.name: rt},
    )
    return Dispatcher(app)


def _create(disp: Dispatcher) -> dict[str, Any]:
    contents = asyncio.run(
        disp.call(
            "odoo_create",
            {"instance": "dev", "model": "res.partner", "values": {"name": "x"}, "dry_run": False},
        )
    )
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


def _last_audit_row(tmp_path: Path) -> dict[str, Any]:
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    return json.loads(lines[-1])  # type: ignore[no-any-return]


def test_dispatcher_reports_outcome_unknown_in_payload_and_audit(tmp_path: Path) -> None:
    err = OdooTransportError("Timeout calling res.partner.create", outcome_unknown=True)
    payload = _create(_build(tmp_path, err))
    assert payload["ok"] is False
    assert payload["error_code"] == "odoo_transport"
    assert payload["outcome"] == "unknown"
    assert payload["hint"].startswith(OUTCOME_UNKNOWN_HINT)

    row = _last_audit_row(tmp_path)
    assert row["tool"] == "odoo_create"
    assert row["result"] == "odoo_transport"
    assert row["details"]["outcome"] == "unknown"


def test_dispatcher_omits_outcome_on_a_plain_failure(tmp_path: Path) -> None:
    """A failure that provably did not commit must not cry wolf."""
    err = OdooRemoteError("Odoo fault on res.partner.create: bad email", server_text=True)
    payload = _create(_build(tmp_path, err))
    assert payload["ok"] is False
    assert "outcome" not in payload
    assert "hint" not in payload
    assert "outcome" not in _last_audit_row(tmp_path)["details"]
