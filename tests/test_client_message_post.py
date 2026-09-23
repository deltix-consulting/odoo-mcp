"""Tests for ``OdooClient.message_post`` argument shaping.

The dispatcher-level tests in ``test_send_message.py`` use a fake
``message_post`` and therefore cannot catch regressions in how
``OdooClient`` builds the kwargs it ships to Odoo over RPC. These
tests pin the wire-level kwargs directly.
"""

from __future__ import annotations

from typing import Any

import pytest

from odoo_mcp.client import OdooClient, OdooRemoteError
from odoo_mcp.config import InstanceConfig
from odoo_mcp.credentials import Credentials
from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD


def _make_client() -> tuple[OdooClient, list[dict[str, Any]]]:
    cfg = InstanceConfig(
        name="dev",
        url="https://dev.example.odoo.com",
        database="dev_db",
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
    # Skip authentication — we only care about how kwargs are built.
    client._uid = 1  # type: ignore[attr-defined]

    captured: list[dict[str, Any]] = []

    def _fake_execute(
        model: str,
        method: str,
        args: list[Any],
        kwargs: dict[str, Any],
    ) -> Any:
        captured.append({"model": model, "method": method, "args": args, "kwargs": kwargs})
        return 4242  # fake mail.message id

    client._execute = _fake_execute  # type: ignore[assignment]
    return client, captured


def test_message_post_sets_body_is_html_true() -> None:
    """``body_is_html=True`` is unconditional so HTML bodies are not escaped.

    Odoo's ``mail.thread.message_post`` HTML-escapes a plain-``str``
    body, expecting a ``markupsafe.Markup`` wrapper for raw HTML —
    a wrapper RPC cannot ship. Without ``body_is_html=True`` an MCP
    caller posting ``"<p>Hi</p>"`` would land in the chatter as
    literal ``&lt;p&gt;Hi&lt;/p&gt;``, and Gmail recipients would see
    the HTML source instead of formatted output.
    """
    client, captured = _make_client()
    result = client.message_post(
        "res.partner",
        7,
        "<p>Hello</p>",
        subject="Hi",
        partner_ids=[10, 11],
        message_type="comment",
    )
    assert result == 4242
    assert len(captured) == 1
    call = captured[0]
    assert call["model"] == "res.partner"
    assert call["method"] == "message_post"
    assert call["args"] == [[7]]
    kwargs = call["kwargs"]
    assert kwargs["body"] == "<p>Hello</p>"
    assert kwargs["body_is_html"] is True
    assert kwargs["message_type"] == "comment"
    assert kwargs["subject"] == "Hi"
    assert kwargs["partner_ids"] == [10, 11]
    assert kwargs["subtype_xmlid"] == "mail.mt_comment"


def test_message_post_log_note_uses_mt_note_and_keeps_body_is_html() -> None:
    """The HTML opt-out applies to log notes too — admin notes can include
    formatted lists / line breaks that would otherwise be escaped."""
    client, captured = _make_client()
    client.message_post(
        "res.partner",
        7,
        "<ul><li>step 1</li></ul>",
        subject=None,
        partner_ids=[],
        message_type="notification",
    )
    kwargs = captured[0]["kwargs"]
    assert kwargs["body_is_html"] is True
    assert kwargs["message_type"] == "notification"
    assert kwargs["subtype_xmlid"] == "mail.mt_note"
    # No subject / no partner_ids ⇒ keys are omitted.
    assert "subject" not in kwargs
    assert "partner_ids" not in kwargs


def test_message_post_rejects_invalid_message_type() -> None:
    """The valid-type guard runs before any RPC, so ``_execute`` is never
    called — protects against silent ``mt_comment`` posts under an
    unexpected ``message_type``."""
    client, captured = _make_client()
    with pytest.raises(OdooRemoteError, match="message_type"):
        client.message_post(
            "res.partner",
            7,
            "hi",
            subject=None,
            partner_ids=[],
            message_type="broadcast",
        )
    assert captured == []
