"""Direct client-level tests for the XML-RPC "cannot marshal None" quirk.

Real-world bug this guards against: Odoo's XML-RPC endpoint serialises
responses with ``allow_none=False`` (it never enabled the nil extension —
see odoo/odoo#12289, #19889, #34037). A workflow/action method that
legitimately returns ``None`` — ``account.move.button_draft``,
``account.payment.action_cancel``, ``sale.order.action_cancel``,
``stock.picking.button_validate`` on a full transfer, etc. — therefore
raises "cannot marshal None" *during response marshalling*, i.e. AFTER
the method already ran and the cursor committed. Our own ServerProxy's
``allow_none=True`` does NOT help: the failure is on Odoo's outbound
serialisation, not our request.

Before the fix, ``_execute`` re-raised this as an ``OdooRemoteError`` and
``odoo_run_document_action`` reported the action as failed — so an agent
could retry a financial action that in fact succeeded (double-post /
double-cancel). The dispatcher-level tests use a fake ``call_document_action``
that never raises the Fault, so they cannot catch this; it needs a direct
client test.
"""

from __future__ import annotations

import xmlrpc.client
from unittest.mock import Mock

import pytest

from odoo_mcp.client import OdooClient, OdooRemoteError
from odoo_mcp.config import InstanceConfig
from odoo_mcp.credentials import Credentials
from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD


def _make_client() -> OdooClient:
    cfg = InstanceConfig(
        name="dev",
        url="https://example.odoo.com",
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
    creds = Credentials(instance_name=cfg.name, username="u", _api_key="k" * 10)
    client = OdooClient(cfg, credentials=creds)
    client._uid = 1  # skip the authenticate round trip; uid is a read-only property
    return client


def test_marshal_none_fault_is_treated_as_void_success() -> None:
    """A "cannot marshal None" fault means the method ran and returned None."""
    client = _make_client()
    mock_proxy = Mock()
    mock_proxy.execute_kw.side_effect = xmlrpc.client.Fault(
        1, "cannot marshal None unless allow_none is enabled"
    )
    client._object = mock_proxy

    # The dispatcher's only caller path for void-returning workflow methods.
    result = client.call_document_action("account.move", "button_draft", [1])

    assert result is None  # void success, NOT an exception
    mock_proxy.execute_kw.assert_called_once()


def test_other_faults_still_raise() -> None:
    """A genuine failure (different fault string) must still surface as an error."""
    client = _make_client()
    mock_proxy = Mock()
    mock_proxy.execute_kw.side_effect = xmlrpc.client.Fault(
        2, "ValidationError: You cannot cancel a posted entry."
    )
    client._object = mock_proxy

    with pytest.raises(OdooRemoteError):
        client.call_document_action("account.move", "button_cancel", [1])
