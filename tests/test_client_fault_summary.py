"""Tests for Odoo XML-RPC fault summarization.

Odoo returns the entire server-side Python traceback in a fault's
``faultString``. :func:`_summarize_odoo_fault` strips that down to the
actionable exception line(s) so MCP clients get a clean, cheap error
instead of 20-40 lines of internal Odoo file paths.
"""

from __future__ import annotations

import xmlrpc.client

import pytest

from odoo_mcp.client import OdooClient, _summarize_odoo_fault
from odoo_mcp.config import InstanceConfig
from odoo_mcp.credentials import Credentials
from odoo_mcp.errors import OdooRemoteError
from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD

_ODOO_TRACEBACK = """Traceback (most recent call last):
  File "/usr/lib/python3/dist-packages/odoo/addons/base/controllers/rpc.py", line 150, in xmlrpc_2
    response = self._xmlrpc(service)
  File "/usr/lib/python3/dist-packages/odoo/http.py", line 1822, in dispatch
    result = self._call_function(**self.request.params)
  File "/usr/lib/python3/dist-packages/odoo/models.py", line 6531, in check_field_access_rights
    raise AccessError(description)
odoo.exceptions.AccessError: You are not allowed to access 'res.partner' (res.partner) records."""


def test_traceback_reduced_to_exception_line() -> None:
    out = _summarize_odoo_fault(_ODOO_TRACEBACK)
    assert out == (
        "odoo.exceptions.AccessError: You are not allowed to access "
        "'res.partner' (res.partner) records."
    )
    assert 'File "' not in out
    assert "Traceback" not in out


def test_multiline_exception_message_preserved() -> None:
    tb = (
        "Traceback (most recent call last):\n"
        '  File "/odoo/models.py", line 10, in create\n'
        "    raise ValidationError(msg)\n"
        "odoo.exceptions.ValidationError: Invalid record:\n"
        "- name is required\n"
        "- email is malformed"
    )
    out = _summarize_odoo_fault(tb)
    assert out == (
        "odoo.exceptions.ValidationError: Invalid record:\n- name is required\n- email is malformed"
    )


def test_chained_traceback_returns_final_exception() -> None:
    tb = (
        "Traceback (most recent call last):\n"
        '  File "/odoo/a.py", line 1, in f\n'
        "    g()\n"
        "KeyError: 'x'\n"
        "\n"
        "During handling of the above exception, another exception occurred:\n"
        "\n"
        "Traceback (most recent call last):\n"
        '  File "/odoo/b.py", line 2, in h\n'
        "    raise UserError('boom')\n"
        "odoo.exceptions.UserError: boom"
    )
    assert _summarize_odoo_fault(tb) == "odoo.exceptions.UserError: boom"


def test_single_line_fault_unchanged() -> None:
    msg = "Some database error: relation does not exist"
    assert _summarize_odoo_fault(msg) == msg


def test_whitespace_only_fault() -> None:
    assert _summarize_odoo_fault("   \n  ") == ""


def test_traceback_marker_without_frames_falls_through() -> None:
    # Degenerate input: the marker line but no recognizable "File" frames.
    # The fault must not be lost — fall through to the stripped original.
    weird = "Traceback (most recent call last):\nSomethingError: huh"
    assert _summarize_odoo_fault(weird) == weird


def _build_client() -> OdooClient:
    cfg = InstanceConfig(
        name="dev",
        url="https://dev.example.odoo.com",
        database="db",
        credentials_env_prefix="ODOO_MCP_DEV",
        production=False,
        timeout_seconds=30,
        max_records_default=50,
        max_records_hard_cap=500,
        rate_limit_per_minute=300,
        allow_self_signed=False,
        allowed_models=frozenset({ALLOWLIST_WILDCARD}),
    )
    creds = Credentials(instance_name="dev", username="u", _api_key="k" * 10)
    client = OdooClient(cfg, credentials=creds)
    client._uid = 1
    return client


class _RaisingProxy:
    """Stand-in for the XML-RPC ServerProxy that always raises a Fault."""

    def execute_kw(self, *args: object, **kwargs: object) -> object:
        raise xmlrpc.client.Fault(1, _ODOO_TRACEBACK)


def test_execute_wraps_fault_with_clean_message() -> None:
    client = _build_client()
    client._object = _RaisingProxy()  # type: ignore[assignment]
    with pytest.raises(OdooRemoteError) as exc_info:
        client._execute("res.partner", "search_read", [[]], {})
    msg = str(exc_info.value)
    assert "Odoo fault on res.partner.search_read:" in msg
    assert "AccessError: You are not allowed" in msg
    assert 'File "' not in msg
