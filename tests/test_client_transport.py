"""Tests for the XML-RPC transport connection lifecycle.

A request that fails mid-flight (timeout after send, protocol error) must
not leave the cached keep-alive connection in the ``Request-sent`` state —
that poisons every subsequent call with ``ResponseNotReady`` until the
process restarts. The transports drop the cached connection on any request
failure so the next call dials fresh.

The transports also own the retry decision: ``Transport.request`` re-sends
the request body once on a dropped connection, which duplicates a
``create`` when the server died *after* committing. Non-idempotent calls
therefore run through ``single_request`` (one attempt only); reads keep the
retry.
"""

from __future__ import annotations

import http.client
import socket
import ssl
import xmlrpc.client

import pytest

from odoo_mcp.client import _RETRY_SAFE_METHODS, _TimeoutSafeTransport, _TimeoutTransport


class _FakeConn:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_transport_drops_cached_connection_after_failed_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _TimeoutTransport(timeout=5.0)
    fake = _FakeConn()
    transport._connection = ("odoo.example.com", fake)

    def boom(
        self: xmlrpc.client.Transport,
        host: object,
        handler: str,
        request_body: bytes,
        verbose: bool = False,
    ) -> object:
        raise http.client.ResponseNotReady("Request-sent")

    monkeypatch.setattr(xmlrpc.client.Transport, "request", boom)
    with pytest.raises(http.client.ResponseNotReady):
        transport.request("odoo.example.com", "/xmlrpc/2/object", b"<xml/>")
    assert fake.closed
    assert transport._connection[1] is None


def test_safe_transport_drops_cached_connection_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _TimeoutSafeTransport(timeout=5.0, context=ssl.create_default_context())
    fake = _FakeConn()
    transport._connection = ("odoo.example.com", fake)

    def boom(
        self: xmlrpc.client.SafeTransport,
        host: object,
        handler: str,
        request_body: bytes,
        verbose: bool = False,
    ) -> object:
        raise TimeoutError("timed out")

    monkeypatch.setattr(xmlrpc.client.SafeTransport, "request", boom)
    with pytest.raises(socket.timeout):
        transport.request("odoo.example.com", "/xmlrpc/2/object", b"<xml/>")
    assert fake.closed
    assert transport._connection[1] is None


def test_transport_successful_request_keeps_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _TimeoutTransport(timeout=5.0)
    fake = _FakeConn()
    transport._connection = ("odoo.example.com", fake)

    def ok(
        self: xmlrpc.client.Transport,
        host: object,
        handler: str,
        request_body: bytes,
        verbose: bool = False,
    ) -> object:
        return ("ok",)

    monkeypatch.setattr(xmlrpc.client.Transport, "request", ok)
    assert transport.request("odoo.example.com", "/xmlrpc/2/object", b"<xml/>") == ("ok",)
    assert not fake.closed
    assert transport._connection[1] is fake


# --- Retry suppression on non-idempotent calls -----------------------------


def _counting_single_request(calls: list[int]) -> object:
    """Build a ``single_request`` stand-in that always drops the connection."""

    def single_request(
        self: xmlrpc.client.Transport,
        host: object,
        handler: str,
        request_body: bytes,
        verbose: bool = False,
    ) -> object:
        calls.append(1)
        raise http.client.RemoteDisconnected("Remote end closed connection without response")

    return single_request


def test_retry_runs_twice_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unsuppressed, the stdlib ``for i in (0, 1)`` loop re-sends once."""
    transport = _TimeoutTransport(timeout=5.0)
    calls: list[int] = []
    monkeypatch.setattr(xmlrpc.client.Transport, "single_request", _counting_single_request(calls))

    with pytest.raises(http.client.RemoteDisconnected):
        transport.request("odoo.example.com", "/xmlrpc/2/object", b"<xml/>")
    assert len(calls) == 2


def test_suppress_retry_sends_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A create that the server committed before dying must not be re-sent."""
    transport = _TimeoutTransport(timeout=5.0)
    calls: list[int] = []
    monkeypatch.setattr(xmlrpc.client.Transport, "single_request", _counting_single_request(calls))

    with pytest.raises(http.client.RemoteDisconnected), transport.suppress_retry():
        transport.request("odoo.example.com", "/xmlrpc/2/object", b"<xml/>")
    assert len(calls) == 1


def test_safe_transport_suppress_retry_sends_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _TimeoutSafeTransport(timeout=5.0, context=ssl.create_default_context())
    calls: list[int] = []
    monkeypatch.setattr(
        xmlrpc.client.SafeTransport, "single_request", _counting_single_request(calls)
    )

    with pytest.raises(http.client.RemoteDisconnected), transport.suppress_retry():
        transport.request("odoo.example.com", "/xmlrpc/2/object", b"<xml/>")
    assert len(calls) == 1


def test_suppress_retry_restores_previous_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard is a scoped context manager, not a latch."""
    transport = _TimeoutTransport(timeout=5.0)
    calls: list[int] = []
    monkeypatch.setattr(xmlrpc.client.Transport, "single_request", _counting_single_request(calls))

    with pytest.raises(http.client.RemoteDisconnected), transport.suppress_retry():
        transport.request("odoo.example.com", "/xmlrpc/2/object", b"<xml/>")
    assert len(calls) == 1

    calls.clear()
    with pytest.raises(http.client.RemoteDisconnected):
        transport.request("odoo.example.com", "/xmlrpc/2/object", b"<xml/>")
    assert len(calls) == 2, "retry must come back after the guard exits"


def test_suppress_retry_still_drops_the_cached_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Suppressing the retry must not regress connection recycling."""
    transport = _TimeoutTransport(timeout=5.0)
    fake = _FakeConn()
    transport._connection = ("odoo.example.com", fake)
    monkeypatch.setattr(xmlrpc.client.Transport, "single_request", _counting_single_request([]))

    with pytest.raises(http.client.RemoteDisconnected), transport.suppress_retry():
        transport.request("odoo.example.com", "/xmlrpc/2/object", b"<xml/>")
    assert fake.closed
    assert transport._connection[1] is None


def test_retry_safe_methods_includes_the_saas19_fallbacks() -> None:
    """The saas-19.x replacements are reads like the methods they replace.

    ``OdooClient.read_group`` / ``diagnose_access`` fall back from
    ``read_group`` / ``check_access_rights`` to ``formatted_read_group`` /
    ``has_access`` once a server reports the legacy method missing. Both
    pairs must sit in the same retry class, or an Odoo Online tenant
    silently loses the dropped-keep-alive retry the moment the fallback
    kicks in.
    """
    for legacy, replacement in (
        ("read_group", "formatted_read_group"),
        ("check_access_rights", "has_access"),
    ):
        assert legacy in _RETRY_SAFE_METHODS
        assert replacement in _RETRY_SAFE_METHODS


def test_retry_safe_methods_excludes_every_write_primitive() -> None:
    """Fail-closed: writes and workflow methods must never be retryable."""
    for method in ("create", "write", "unlink", "message_post"):
        assert method not in _RETRY_SAFE_METHODS
    # Workflow methods come from security.document_actions and are arbitrary.
    for method in ("button_confirm", "action_post", "button_validate"):
        assert method not in _RETRY_SAFE_METHODS
