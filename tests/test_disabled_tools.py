"""Tests for the ODOO_MCP_DISABLE_TOOLS env var.

Hides specific tools from the MCP ``tools/list`` advertisement so a
client never sees them — defense-in-depth on top of the per-tool
allowlist + read-only session toggle.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import pytest
from mcp.types import ListToolsRequest

from odoo_mcp import server


def test_no_env_returns_full_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ODOO_MCP_DISABLE_TOOLS", raising=False)
    assert server._disabled_tools() == frozenset()


def test_env_parses_comma_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_MCP_DISABLE_TOOLS", "odoo_create,odoo_write")
    assert server._disabled_tools() == frozenset({"odoo_create", "odoo_write"})


def test_env_tolerates_whitespace_and_empties(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_MCP_DISABLE_TOOLS", " odoo_create , , odoo_archive_or_delete  ")
    assert server._disabled_tools() == frozenset({"odoo_create", "odoo_archive_or_delete"})


def _list_tools_via_server(srv: Any) -> list[str]:
    """Invoke the registered ListToolsRequest handler and return tool names.

    Uses the same wire path the MCP runtime would: pull the handler out
    of ``request_handlers``, hand it a ``ListToolsRequest``, and inspect
    the resulting tool list. This is the live filter, not a re-run of
    the filter logic.
    """
    handler = srv.request_handlers[ListToolsRequest]
    request = ListToolsRequest(method="tools/list")
    result = asyncio.run(handler(request))
    # ``handler`` returns a ``ServerResult`` wrapper; the inner value is
    # a ``ListToolsResult`` with a ``.tools`` list.
    inner = result.root
    return [t.name for t in inner.tools]


def test_disable_filters_advertised_tools_list(
    monkeypatch: pytest.MonkeyPatch,
    make_app: Callable[..., Any],
) -> None:
    monkeypatch.setenv("ODOO_MCP_DISABLE_TOOLS", "odoo_create,odoo_write")
    app = make_app()
    srv = server.build_server(app)
    advertised = _list_tools_via_server(srv)
    assert "odoo_create" not in advertised
    assert "odoo_write" not in advertised
    # Other tools survive.
    assert "odoo_search_read" in advertised
    assert "odoo_help" in advertised


def test_no_env_advertises_full_tool_list(
    monkeypatch: pytest.MonkeyPatch,
    make_app: Callable[..., Any],
) -> None:
    monkeypatch.delenv("ODOO_MCP_DISABLE_TOOLS", raising=False)
    monkeypatch.delenv("ODOO_MCP_ENABLE_EXTERNAL_COMMS", raising=False)
    app = make_app()
    srv = server.build_server(app)
    advertised = set(_list_tools_via_server(srv))
    # Every tool from build_tools() must show up EXCEPT odoo_send_message,
    # which is double-gated: tool only advertises when (a) the env var
    # is set and (b) at least one instance has external_comms_enabled.
    # Both are off in this fixture, so the tool is hidden.
    from odoo_mcp.tools import build_tools

    expected = {t.name for t in build_tools()} - {"odoo_send_message"}
    assert advertised == expected


def test_unknown_disable_names_logged_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    make_app: Callable[..., Any],
) -> None:
    monkeypatch.setenv("ODOO_MCP_DISABLE_TOOLS", "odoo_create,not_a_real_tool")
    caplog.set_level(logging.WARNING, logger="odoo_mcp.server")
    app = make_app()
    srv = server.build_server(app)
    msgs = " ".join(r.message for r in caplog.records)
    assert "not_a_real_tool" in msgs
    # The real tool is still hidden; the bogus name is silently ignored
    # in the filter.
    advertised = _list_tools_via_server(srv)
    assert "odoo_create" not in advertised
