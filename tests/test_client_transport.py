"""Tests for the XML-RPC transport connection lifecycle.

A request that fails mid-flight (timeout after send, protocol error) must
not leave the cached keep-alive connection in the ``Request-sent`` state —
that poisons every subsequent call with ``ResponseNotReady`` until the
process restarts. The transports drop the cached connection on any request
failure so the next call dials fresh.
"""

from __future__ import annotations

import http.client
import socket
import ssl
import xmlrpc.client

import pytest

from odoo_mcp.client import _TimeoutSafeTransport, _TimeoutTransport


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
