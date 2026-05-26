"""Tests for HTTPS_PROXY support in the XML-RPC transport.

Real-world bug this guards against: stock ``xmlrpc.client`` does NOT
honor ``HTTPS_PROXY`` (unlike ``urllib.request``). Our previous
``_TimeoutSafeTransport`` was a thin timeout-adding subclass, so it
inherited the same gap. Every container running odoo-mcp behind a
Squid + iptables egress allowlist (the now-standard tenant shape)
silently 30-second-timed-out trying to reach Odoo directly.

These tests pin two things:

1. :func:`_resolve_proxy` — the pure env-var / NO_PROXY parser. Cheap
   to test exhaustively because it has no side effects.
2. :meth:`_TimeoutSafeTransport.make_connection` — that when a proxy
   is configured, the returned :class:`http.client.HTTPSConnection`
   has ``_tunnel_host`` set (i.e. ``set_tunnel`` was called), and
   that without a proxy the same code path returns a direct
   connection. We don't open a real socket — that's an integration
   concern out of scope for unit tests.
"""

from __future__ import annotations

import ssl

import pytest

from odoo_mcp.client import _resolve_proxy, _TimeoutSafeTransport

# ---------------------------------------------------------------------------
# _resolve_proxy: env var resolution
# ---------------------------------------------------------------------------


def test_resolve_proxy_returns_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    assert _resolve_proxy("https", "deltix.odoo.com") is None


def test_resolve_proxy_reads_https_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://squid:3128")
    result = _resolve_proxy("https", "deltix.odoo.com")
    assert result == ("squid", 3128, {})


def test_resolve_proxy_lowercase_wins_over_uppercase(monkeypatch: pytest.MonkeyPatch) -> None:
    """urllib precedence: lowercase env var takes priority. Match it."""
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://uppercase:8080")
    monkeypatch.setenv("https_proxy", "http://lowercase:3128")
    host, port, _ = _resolve_proxy("https", "x.odoo.com")  # type: ignore[misc]
    assert (host, port) == ("lowercase", 3128)


def test_resolve_proxy_picks_https_for_https_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTPS_PROXY must NOT be used for an http:// target — that's HTTP_PROXY's job."""
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://https-proxy:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://http-proxy:8080")
    https = _resolve_proxy("https", "x.odoo.com")
    http = _resolve_proxy("http", "x.odoo.com")
    assert https is not None and https[0] == "https-proxy"
    assert http is not None and http[0] == "http-proxy"


def test_resolve_proxy_honors_no_proxy_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://squid:3128")
    monkeypatch.setenv("NO_PROXY", "deltix.odoo.com,localhost")
    assert _resolve_proxy("https", "deltix.odoo.com") is None
    # A different host still proxies.
    assert _resolve_proxy("https", "other.odoo.com") is not None


def test_resolve_proxy_honors_no_proxy_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    """A NO_PROXY entry of ``odoo.com`` must cover ``deltix.odoo.com`` too —
    that's how curl, requests, and urllib all interpret it."""
    monkeypatch.setenv("HTTPS_PROXY", "http://squid:3128")
    monkeypatch.setenv("NO_PROXY", ".odoo.com")
    assert _resolve_proxy("https", "deltix.odoo.com") is None
    assert _resolve_proxy("https", "odoo.com") is None
    # Different domain still proxies.
    assert _resolve_proxy("https", "other.example.org") is not None


def test_resolve_proxy_no_proxy_star_disables_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://squid:3128")
    monkeypatch.setenv("NO_PROXY", "*")
    assert _resolve_proxy("https", "anything") is None


def test_resolve_proxy_extracts_basic_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Authenticated proxies are supported via embedded userinfo."""
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://alice:secret@squid:3128")
    host, port, headers = _resolve_proxy("https", "x.odoo.com")  # type: ignore[misc]
    assert (host, port) == ("squid", 3128)
    # The actual header value is base64("alice:secret"); we don't
    # hardcode it here — just verify it's a Basic auth header.
    assert headers["Proxy-Authorization"].startswith("Basic ")
    # And the secret value is NOT echoed in cleartext.
    assert "secret" not in headers["Proxy-Authorization"]


def test_resolve_proxy_tolerates_bare_host_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """``HTTPS_PROXY=squid:3128`` (no scheme) is a common misconfig.
    Don't crash — assume http:// and proceed."""
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "squid:3128")
    result = _resolve_proxy("https", "x.odoo.com")
    assert result is not None
    assert result[0] == "squid"
    assert result[1] == 3128


# ---------------------------------------------------------------------------
# Transport integration: did we actually call set_tunnel?
# ---------------------------------------------------------------------------


def test_transport_uses_tunnel_when_proxy_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """The end-to-end guarantee. With HTTPS_PROXY set, the connection
    returned by make_connection must be configured to tunnel through
    the proxy (``_tunnel_host`` populated) and connected to the proxy
    host:port (not the Odoo host)."""
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://squid:3128")
    transport = _TimeoutSafeTransport(timeout=5.0, context=ssl.create_default_context())
    conn = transport.make_connection("deltix.odoo.com:443")
    # The TCP socket will go to the proxy …
    assert conn.host == "squid"
    assert conn.port == 3128
    # … and tunnel to the Odoo host.
    assert conn._tunnel_host == "deltix.odoo.com"
    assert conn._tunnel_port == 443


def test_transport_direct_when_no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """No proxy → direct connection to Odoo, no tunnel.

    Regression guard against an over-eager refactor wiring set_tunnel
    unconditionally and breaking the no-proxy path for everyone.
    """
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    transport = _TimeoutSafeTransport(timeout=5.0, context=ssl.create_default_context())
    conn = transport.make_connection("deltix.odoo.com:443")
    assert conn.host == "deltix.odoo.com"
    assert conn.port == 443
    assert conn._tunnel_host is None


def test_transport_direct_when_target_in_no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """NO_PROXY beats HTTPS_PROXY. Verify at the transport level too —
    the resolver test is necessary but not sufficient; this catches a
    refactor that forgets to plumb the resolver into make_connection."""
    monkeypatch.setenv("HTTPS_PROXY", "http://squid:3128")
    monkeypatch.setenv("NO_PROXY", ".odoo.com")
    transport = _TimeoutSafeTransport(timeout=5.0, context=ssl.create_default_context())
    conn = transport.make_connection("deltix.odoo.com:443")
    assert conn.host == "deltix.odoo.com"
    assert conn._tunnel_host is None
