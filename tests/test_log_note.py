"""Tests for ``odoo_log_note`` — internal log note posting.

The tool exists because the v0.20.x ``odoo_send_message`` flow is
two-gates-deep (env var + per-instance config) because of the email
risk on ``message_type='comment'``. Pieterjan's audit-trail use case
needs ONLY the log-note flavour: hardcoded notification + mt_note +
empty partner_ids → physically can't email. That guarantee earns it
a separate tool with a smaller security envelope and no opt-in
gates.

These tests pin both ends of the contract:

- Operation classification: must be a write op (prod-guard applies).
- Tool registered next to send_message in stable order.
- The dispatcher forces ``message_type='notification'`` + empty
  ``partner_ids`` regardless of what the caller did or didn't pass.
- The tool works WITHOUT ``external_comms_enabled`` — that's the
  whole point. Pinned so a refactor adding the gate is loud.
- ``model`` still flows through allowlist + write-blocklist so
  ``odoo_log_note`` on ``mail.message`` is refused.
- Payload digest binds to ``(record_id, body)``; a body swap
  between preview and commit is refused.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from odoo_mcp.audit import AuditLog
from odoo_mcp.client import OdooClient
from odoo_mcp.config import AppConfig, Defaults, InstanceConfig
from odoo_mcp.credentials import Credentials
from odoo_mcp.dispatcher import Dispatcher, InstanceRuntime, OdooMcpApp
from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD, Operation, is_read, is_write
from odoo_mcp.security.limits import RateLimiter
from odoo_mcp.security.prod_guard import ProdGuard
from odoo_mcp.tools import build_tools


class _LogNoteFake:
    """Records every message_post call so the test can pin the args.

    Critical: must capture ``message_type``, ``partner_ids``, and
    ``subject`` to prove the dispatcher hardcodes them (no email path
    reachable through this tool regardless of caller input).
    """

    def __init__(self, *, message_id: int = 4242) -> None:
        self.message_id = message_id
        self.message_post_calls: list[dict[str, Any]] = []
        self.is_admin: bool | None = False
        self.admin_reason: str | None = None
        self.username = "u"
        self.uid = 7

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        return {}

    def message_post(
        self,
        model: str,
        record_id: int,
        body: str,
        *,
        subject: str | None,
        partner_ids: list[int],
        message_type: str,
    ) -> int:
        self.message_post_calls.append(
            {
                "model": model,
                "record_id": record_id,
                "body": body,
                "subject": subject,
                "partner_ids": list(partner_ids),
                "message_type": message_type,
            }
        )
        return self.message_id


def _instance_config(
    *, production: bool = False, external_comms_enabled: bool = False
) -> InstanceConfig:
    return InstanceConfig(
        name="prod" if production else "dev",
        url="https://example.odoo.com",
        database="db",
        credentials_env_prefix="ODOO_MCP_PROD" if production else "ODOO_MCP_DEV",
        production=production,
        timeout_seconds=30,
        max_records_default=50,
        max_records_hard_cap=500,
        rate_limit_per_minute=300,
        allow_self_signed=False,
        allowed_models=frozenset({ALLOWLIST_WILDCARD}),
        external_comms_enabled=external_comms_enabled,
    )


def _build(
    tmp_path: Path,
    fake: _LogNoteFake,
    *,
    production: bool = False,
    external_comms_enabled: bool = False,
) -> OdooMcpApp:
    cfg = _instance_config(production=production, external_comms_enabled=external_comms_enabled)
    creds = Credentials(instance_name=cfg.name, username="u", _api_key="k" * 10)
    real = OdooClient(cfg, credentials=creds)
    app_cfg = AppConfig(
        path=tmp_path / "config.toml",
        defaults=Defaults(),
        instances={cfg.name: cfg},
        audit_log_path=tmp_path / "audit.jsonl",
    )
    audit = AuditLog(app_cfg.audit_log_path)
    rl = RateLimiter()
    rl.configure(cfg.name, cfg.rate_limit_per_minute)
    rt = InstanceRuntime(config=cfg, client=real)
    rt.client = fake  # type: ignore[assignment]
    return OdooMcpApp(
        config=app_cfg,
        audit=audit,
        prod_guard=ProdGuard(),
        rate_limiter=rl,
        instances={cfg.name: rt},
    )


def _call(disp: Dispatcher, args: dict[str, Any]) -> dict[str, Any]:
    contents = asyncio.run(disp.call("odoo_log_note", args))
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Operation + tool registration
# ---------------------------------------------------------------------------


def test_operation_is_write_op() -> None:
    """Hard guarantee: a refactor that drops the new Operation into
    _READ_OPS would silently bypass the prod-guard for log notes."""
    assert is_write(Operation.LOG_NOTE)
    assert not is_read(Operation.LOG_NOTE)


def test_tool_registered_next_to_send_message() -> None:
    """Operators (and the agent) discover log_note as the "send_message
    sibling for notes" — keep it adjacent in the tool listing."""
    names = [t.name for t in build_tools()]
    assert "odoo_log_note" in names
    idx_send = names.index("odoo_send_message")
    idx_log = names.index("odoo_log_note")
    assert idx_log == idx_send + 1


# ---------------------------------------------------------------------------
# The whole point: works without external_comms_enabled
# ---------------------------------------------------------------------------


def test_log_note_works_without_external_comms_enabled(tmp_path: Path) -> None:
    """The defining contract. ``odoo_send_message`` requires the
    external_comms two-gate guard because it CAN email; log notes
    cannot email, so the guard would just block a useful local
    audit trail. Pin that no gate is required."""
    fake = _LogNoteFake()
    app = _build(tmp_path, fake, external_comms_enabled=False)
    out = _call(
        Dispatcher(app),
        {
            "instance": "dev",
            "model": "sale.order",
            "record_id": 42,
            "body": "Spoke to customer; PO replaced by 4711.",
        },
    )
    assert out["committed"] is True
    assert out["message_id"] == 4242
    # And the actual Odoo call carried the hardcoded note shape.
    assert len(fake.message_post_calls) == 1
    call = fake.message_post_calls[0]
    assert call["message_type"] == "notification"
    assert call["partner_ids"] == []
    assert call["subject"] is None
    assert call["body"] == "Spoke to customer; PO replaced by 4711."


def test_log_note_hardcodes_notification_type_even_if_caller_lies(tmp_path: Path) -> None:
    """The schema only accepts (instance, model, record_id, body) plus
    the dry-run/token pair — there is no ``message_type`` knob. But
    pin the dispatcher behaviour explicitly: even if a future schema
    change exposes the field, the dispatcher must still force
    notification + empty partners. Otherwise we'd ship an email vector
    through the no-gate tool."""
    fake = _LogNoteFake()
    app = _build(tmp_path, fake)
    _call(
        Dispatcher(app),
        {
            "instance": "dev",
            "model": "sale.order",
            "record_id": 1,
            "body": "x",
            # Hostile extra inputs that the schema rejects today;
            # belt-and-braces in case a refactor relaxes the schema.
            "message_type": "comment",
            "partner_ids": [99],
            "subject": "Outage notice",
        },
    )
    # Last call is the only one; check the hardcoded shape held.
    call = fake.message_post_calls[-1]
    assert call["message_type"] == "notification"
    assert call["partner_ids"] == []
    assert call["subject"] is None


# ---------------------------------------------------------------------------
# Dry-run preview
# ---------------------------------------------------------------------------


def test_dry_run_returns_preview_with_token(tmp_path: Path) -> None:
    fake = _LogNoteFake()
    app = _build(tmp_path, fake, production=True)
    app.prod_guard.unlock("prod", production=True)
    out = _call(
        Dispatcher(app),
        {
            "instance": "prod",
            "model": "res.partner",
            "record_id": 7,
            "body": "Reviewed the SLA terms with the customer.",
            "dry_run": True,
        },
    )
    assert out["preview"] is True
    assert out["model"] == "res.partner"
    assert out["record_id"] == 7
    # The preview must SHOW the body verbatim so the operator can
    # review it before committing.
    assert "SLA terms" in out["body_preview"]
    # And it must make the "no email" guarantee explicit so the
    # operator doesn't conflate this with send_message.
    assert out["would_send_email"] is False
    assert out["confirmation_token"].startswith("conf_")
    # No message was actually posted during the preview.
    assert fake.message_post_calls == []


def test_dry_run_truncates_long_body_in_preview(tmp_path: Path) -> None:
    """A multi-page log note shouldn't fill the operator's screen at
    preview time. Same 2000-char truncation as ``send_message``."""
    fake = _LogNoteFake()
    app = _build(tmp_path, fake, production=True)
    app.prod_guard.unlock("prod", production=True)
    body = "A" * 5000
    out = _call(
        Dispatcher(app),
        {
            "instance": "prod",
            "model": "res.partner",
            "record_id": 1,
            "body": body,
            "dry_run": True,
        },
    )
    assert "[truncated]" in out["body_preview"]
    assert len(out["body_preview"]) < len(body)


# ---------------------------------------------------------------------------
# Allowlist + write-blocklist still apply
# ---------------------------------------------------------------------------


def test_refuses_write_blocklisted_model(tmp_path: Path) -> None:
    """Logging on ``mail.message`` itself is refused — the write-
    blocklist applies because posting a note to a message record
    is semantically a write against that message."""
    fake = _LogNoteFake()
    app = _build(tmp_path, fake)
    out = _call(
        Dispatcher(app),
        {
            "instance": "dev",
            "model": "mail.message",
            "record_id": 1,
            "body": "x",
        },
    )
    assert out["ok"] is False
    assert "read-only" in out["error"].lower() or "blocklist" in out["error"].lower()
    assert fake.message_post_calls == []


# ---------------------------------------------------------------------------
# Prod flow + payload-digest binding
# ---------------------------------------------------------------------------


def test_prod_requires_dry_run_then_token(tmp_path: Path) -> None:
    fake = _LogNoteFake()
    app = _build(tmp_path, fake, production=True)
    app.prod_guard.unlock("prod", production=True)
    disp = Dispatcher(app)

    preview = _call(
        disp,
        {
            "instance": "prod",
            "model": "sale.order",
            "record_id": 11,
            "body": "Confirmed cancel reason with finance.",
            "dry_run": True,
        },
    )
    token = preview["confirmation_token"]
    result = _call(
        disp,
        {
            "instance": "prod",
            "model": "sale.order",
            "record_id": 11,
            "body": "Confirmed cancel reason with finance.",
            "dry_run": False,
            "confirmation_token": token,
        },
    )
    assert result["committed"] is True
    assert result["message_id"] == 4242
    # Exactly one message_post call — the dry-run did not also fire one.
    assert len(fake.message_post_calls) == 1


def test_token_rejects_body_swap(tmp_path: Path) -> None:
    """Payload-digest contract: preview-with-body-A cannot commit with
    body-B using the same token. The v0.18.0 token-binding fix applied
    to log notes via the ``(record_id, body)`` digest key set."""
    fake = _LogNoteFake()
    app = _build(tmp_path, fake, production=True)
    app.prod_guard.unlock("prod", production=True)
    disp = Dispatcher(app)
    preview = _call(
        disp,
        {
            "instance": "prod",
            "model": "sale.order",
            "record_id": 11,
            "body": "Quick reminder.",
            "dry_run": True,
        },
    )
    token = preview["confirmation_token"]
    swapped = _call(
        disp,
        {
            "instance": "prod",
            "model": "sale.order",
            "record_id": 11,
            "body": "Customer agreed to a 100% discount — bookkeeping note.",
            "dry_run": False,
            "confirmation_token": token,
        },
    )
    assert swapped["ok"] is False
    assert "different payload" in swapped["error"]
    assert fake.message_post_calls == []
