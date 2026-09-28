"""A failed tool call must reach the client with MCP's ``isError: true``.

The MCP spec reports tool-execution errors *in the result* with
``isError: true`` so the model (and any client or agent framework that
branches on the flag) can tell a refusal from a success. deltix's
dispatcher always put ``ok: false`` in its JSON envelope, but returned
that envelope as ordinary content — so the SDK wrapped every refusal,
prod-guard block and Odoo fault as ``isError: false``.

These tests drive the REAL ``CallToolRequest`` handler that
``build_server`` registers with the SDK (the same wire path the stdio
loop uses), not ``Dispatcher.call`` — the flag is set at the SDK
boundary, so only that boundary can prove it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest
from mcp.types import CallToolRequest, CallToolRequestParams, CallToolResult

from odoo_mcp import dispatcher as dispatcher_mod
from odoo_mcp import server


def _call_via_server(srv: Any, name: str, arguments: dict[str, Any]) -> CallToolResult:
    handler = srv.request_handlers[CallToolRequest]
    request = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name=name, arguments=arguments),
    )
    result = asyncio.run(handler(request)).root
    assert isinstance(result, CallToolResult)
    return result


def _envelope(result: CallToolResult) -> dict[str, Any]:
    assert len(result.content) == 1
    text = result.content[0].text  # type: ignore[union-attr]
    payload = json.loads(text)
    assert isinstance(payload, dict)
    return payload


@pytest.fixture
def srv(monkeypatch: pytest.MonkeyPatch, make_app: Callable[..., Any]) -> Any:
    monkeypatch.delenv("ODOO_MCP_DISABLE_TOOLS", raising=False)
    return server.build_server(make_app())


def test_successful_call_is_not_flagged(srv: Any) -> None:
    result = _call_via_server(srv, "odoo_help", {})
    assert result.isError is False
    assert _envelope(result)["ok"] is True


def test_dispatcher_refusal_sets_is_error(srv: Any) -> None:
    result = _call_via_server(
        srv, "odoo_search_read", {"instance": "no-such-instance", "model": "res.partner"}
    )
    payload = _envelope(result)
    assert payload["ok"] is False
    assert result.isError is True


def test_disabled_tool_refusal_sets_is_error(
    monkeypatch: pytest.MonkeyPatch, make_app: Callable[..., Any]
) -> None:
    # The operator's ODOO_MCP_DISABLE_TOOLS directive is a call-time
    # refusal; a client must be told it did not run.
    monkeypatch.setenv("ODOO_MCP_DISABLE_TOOLS", "odoo_create")
    srv = server.build_server(make_app())
    result = _call_via_server(
        srv, "odoo_create", {"instance": "dev", "model": "res.partner", "values": {"name": "x"}}
    )
    assert _envelope(result)["ok"] is False
    assert result.isError is True


def test_last_resort_internal_error_sets_is_error(
    monkeypatch: pytest.MonkeyPatch, srv: Any
) -> None:
    def boom(self: Any, args: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("unexpected")

    monkeypatch.setitem(dispatcher_mod._HANDLERS, "odoo_help", boom)
    result = _call_via_server(srv, "odoo_help", {})
    payload = _envelope(result)
    assert payload["error_code"] == "internal_error"
    assert result.isError is True


def test_error_text_is_byte_identical_to_the_envelope(
    make_app: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Setting the flag must not change a byte of what the model reads:
    # the SDK's error path builds the content from str(exception), so
    # the envelope has to survive that round trip verbatim.
    monkeypatch.delenv("ODOO_MCP_DISABLE_TOOLS", raising=False)
    app = make_app()
    args = {"instance": "no-such-instance", "model": "res.partner"}
    direct = asyncio.run(dispatcher_mod.Dispatcher(app).call("odoo_search_read", args))
    via_server = _call_via_server(server.build_server(app), "odoo_search_read", args)
    assert via_server.content[0].text == direct[0].text  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("odoo_help", {}),
        ("odoo_list_instances", {}),
        ("odoo_search_read", {"instance": "no-such-instance", "model": "res.partner"}),
        ("not_a_tool", {}),
    ],
)
def test_status_flag_matches_envelope_ok(
    make_app: Callable[..., Any], name: str, arguments: dict[str, Any]
) -> None:
    content, failed = asyncio.run(
        dispatcher_mod.Dispatcher(make_app()).call_with_status(name, arguments)
    )
    assert failed is (json.loads(content[0].text)["ok"] is False)
