"""Integers above 2**31 - 1 reach Odoo as ``<i8>`` instead of failing locally.

The stdlib marshaller raises ``OverflowError`` for any int outside the int4
range before the request is sent, so a domain or a write carrying an amount
like 5,000,000,000 (common in IDR / VND / KRW) ended in ``internal_error``.

The end-to-end tests use a real loopback ``SimpleXMLRPCServer``: it parses
requests with ``xmlrpc.client.loads``, the same call Odoo's XML-RPC
controller makes on 16.0 through master.
"""

from __future__ import annotations

import threading
import xmlrpc.client
from collections.abc import Iterator
from typing import Any
from xmlrpc.server import SimpleXMLRPCRequestHandler, SimpleXMLRPCServer

import pytest

from odoo_mcp._xmlrpc_int8 import IntegerOutOfRangeError, OdooServerProxy, dumps_call
from odoo_mcp.client import OdooClient
from odoo_mcp.credentials import Credentials
from odoo_mcp.errors import LimitExceededError, OdooMcpError

FIVE_BILLION = 5_000_000_000


class _AnyPathHandler(SimpleXMLRPCRequestHandler):
    rpc_paths = ()  # accept /xmlrpc/2/common and /xmlrpc/2/object alike

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


class _FakeOdoo:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, list[Any], dict[str, Any]]] = []

    def authenticate(self, db: str, login: str, key: str, ctx: dict[str, Any]) -> int:
        return 7

    def execute_kw(
        self,
        db: str,
        uid: int,
        key: str,
        model: str,
        method: str,
        args: list[Any],
        kwargs: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append((model, method, args, kwargs or {}))
        if method == "search_read":
            return []
        return True


@pytest.fixture
def fake_odoo(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[_FakeOdoo, str]]:
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "*")
    server = SimpleXMLRPCServer(
        ("127.0.0.1", 0), requestHandler=_AnyPathHandler, allow_none=True, logRequests=False
    )
    odoo = _FakeOdoo()
    server.register_function(odoo.authenticate, "authenticate")
    server.register_function(odoo.execute_kw, "execute_kw")
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.daemon = True
    thread.start()
    try:
        yield odoo, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _client(make_instance_config: Any, url: str) -> OdooClient:
    cfg = make_instance_config(url=url)
    creds = Credentials(instance_name=cfg.name, username="u", _api_key="k" * 10)
    return OdooClient(cfg, creds)


# ---------------------------------------------------------------------------
# Why this module exists
# ---------------------------------------------------------------------------


def test_stock_marshaller_still_refuses_int8() -> None:
    """If this ever passes without raising, the stdlib learned <i8> and the
    override can go."""
    with pytest.raises(OverflowError):
        xmlrpc.client.dumps((FIVE_BILLION,), "execute_kw")


def test_stdlib_server_proxy_internals_still_exist() -> None:
    """OdooServerProxy overrides ``_ServerProxy__request`` and reads these
    name-mangled attributes. Pin them so a CPython change fails here."""
    assert hasattr(xmlrpc.client.ServerProxy, "_ServerProxy__request")
    proxy = xmlrpc.client.ServerProxy("http://127.0.0.1:1/x", allow_none=True)
    for attr in ("encoding", "allow_none", "transport", "host", "handler", "verbose"):
        assert f"_ServerProxy__{attr}" in vars(proxy), attr


# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------


def test_int4_values_keep_the_plain_int_tag() -> None:
    body = dumps_call((1, -(1 << 31), (1 << 31) - 1), "execute_kw", allow_none=True)
    assert "<i8>" not in body
    assert body.count("<int>") == 3


def test_values_past_int4_use_the_i8_tag_and_round_trip() -> None:
    values = (1 << 31, -(1 << 31) - 1, FIVE_BILLION, (1 << 63) - 1, -(1 << 63))
    body = dumps_call(values, "execute_kw", allow_none=True)
    assert body.count("<i8>") == len(values)
    params, method = xmlrpc.client.loads(body)
    assert method == "execute_kw"
    assert params == values
    assert all(type(v) is int for v in params)


def test_bool_and_none_are_untouched() -> None:
    body = dumps_call((True, False, None), "execute_kw", allow_none=True)
    assert "<boolean>1</boolean>" in body
    assert "<boolean>0</boolean>" in body
    assert "<nil/>" in body
    assert xmlrpc.client.loads(body)[0] == (True, False, None)


def test_request_shape_matches_stdlib_for_small_values() -> None:
    params = ("db", 7, "key", "res.partner", "search_read", [[["id", "=", 1]]], {"limit": 5})
    assert dumps_call(params, "execute_kw", allow_none=True) == xmlrpc.client.dumps(
        params, "execute_kw", allow_none=True
    )


def test_nested_big_ints_inside_domains_and_dicts() -> None:
    params = ([[["amount_total", ">", FIVE_BILLION]]], {"vals": {"x": [FIVE_BILLION]}})
    body = dumps_call(params, "execute_kw", allow_none=True)
    assert xmlrpc.client.loads(body)[0] == params


# ---------------------------------------------------------------------------
# Past int8: a typed refusal, not internal_error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [1 << 63, -(1 << 63) - 1, 10**20])
def test_past_int8_is_a_limit_error_without_the_value(value: int) -> None:
    with pytest.raises(IntegerOutOfRangeError) as info:
        dumps_call(([["x", "=", value]],), "execute_kw", allow_none=True)
    err = info.value
    assert isinstance(err, LimitExceededError)
    assert isinstance(err, OdooMcpError)
    assert err.code == "limit_exceeded"
    assert str(value) not in err.user_message
    assert str(value) not in err.audit_message
    assert err.hint and "decimal" in err.hint


# ---------------------------------------------------------------------------
# End to end through OdooClient
# ---------------------------------------------------------------------------


def test_client_uses_the_int8_proxy(make_instance_config: Any) -> None:
    client = _client(make_instance_config, "https://example.odoo.com")
    assert isinstance(client._object, OdooServerProxy)
    assert isinstance(client._common, OdooServerProxy)


def test_write_of_five_billion_reaches_odoo_exactly(
    make_instance_config: Any, fake_odoo: tuple[_FakeOdoo, str]
) -> None:
    odoo, url = fake_odoo
    client = _client(make_instance_config, url)
    client.authenticate()
    assert client.write("crm.lead", [3], {"expected_revenue": FIVE_BILLION}) is True
    model, method, args, _ = odoo.calls[-1]
    assert (model, method) == ("crm.lead", "write")
    assert args == [[3], {"expected_revenue": FIVE_BILLION}]
    assert type(args[1]["expected_revenue"]) is int


def test_search_read_domain_with_big_int_reaches_odoo(
    make_instance_config: Any, fake_odoo: tuple[_FakeOdoo, str]
) -> None:
    odoo, url = fake_odoo
    client = _client(make_instance_config, url)
    client.authenticate()
    domain = [["amount_total", ">=", FIVE_BILLION]]
    client.search_read("sale.order", domain, ["name"], limit=5, offset=0, order=None)
    model, method, args, _ = odoo.calls[-1]
    assert (model, method) == ("sale.order", "search_read")
    assert args[0] == domain


def test_past_int8_is_refused_before_anything_is_sent(
    make_instance_config: Any, fake_odoo: tuple[_FakeOdoo, str]
) -> None:
    odoo, url = fake_odoo
    client = _client(make_instance_config, url)
    client.authenticate()
    before = len(odoo.calls)
    with pytest.raises(IntegerOutOfRangeError):
        client.write("crm.lead", [3], {"expected_revenue": 10**20})
    assert len(odoo.calls) == before
    # The connection is still usable afterwards.
    assert client.write("crm.lead", [3], {"expected_revenue": FIVE_BILLION}) is True
