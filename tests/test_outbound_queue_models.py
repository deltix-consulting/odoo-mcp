"""Outbound delivery-queue models are write-blocklisted.

Odoo delivers whatever sits in its outbound queues on a cron, with no
further method call: a ``mail.mail`` row in state ``outgoing`` (the
default) is emailed by ``ir_cron_mail_scheduler_action`` within a minute,
a ``mail.scheduled.message`` is posted on its record's chatter (with
email notifications to ``partner_ids``) by ``ir_cron_post_scheduled_message``,
and so on. A generic ``odoo_create`` on one of these models is therefore
an outbound message that skipped every gate ``odoo_send_message`` carries:
both ``external_comms`` opt-ins, the mandatory dry-run, the recipient
confirmation. ``mail.mail`` additionally ``_inherits`` ``mail.message``, so
creating or writing one creates or edits a chatter message with any author
— the exact forgery ``mail.message`` is write-blocklisted to prevent.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from odoo_mcp._odoo_reference import ODOO_STANDARD_MODELS
from odoo_mcp.audit import AuditLog
from odoo_mcp.client import OdooClient
from odoo_mcp.config import AppConfig, Defaults, InstanceConfig
from odoo_mcp.credentials import Credentials
from odoo_mcp.dispatcher import Dispatcher, InstanceRuntime, OdooMcpApp
from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD, MODEL_WRITE_BLOCKLIST, check_model
from odoo_mcp.security.limits import RateLimiter
from odoo_mcp.security.prod_guard import ProdGuard

# (model, a payload that would deliver something once the cron runs)
_QUEUE_PAYLOADS: list[tuple[str, dict[str, Any]]] = [
    (
        "mail.mail",
        {"email_to": "someone@example.com", "subject": "s", "body_html": "<p>x</p>"},
    ),
    (
        "mail.scheduled.message",
        {
            "model": "crm.lead",
            "res_id": 1,
            "author_id": 3,
            "partner_ids": [(6, 0, [7])],
            "body": "<p>x</p>",
            "scheduled_date": "2099-01-01 00:00:00",
        },
    ),
    ("mail.message.schedule", {"mail_message_id": 5, "scheduled_datetime": "2099-01-01"}),
    ("sms.sms", {"number": "+3200000000", "body": "x"}),
    ("snailmail.letter", {"model": "account.move", "res_id": 1, "partner_id": 7}),
]
_QUEUE_MODELS = [m for m, _ in _QUEUE_PAYLOADS]

_META: dict[str, dict[str, dict[str, Any]]] = {
    "res.partner": {
        "id": {"type": "integer"},
        "name": {"type": "char"},
        "x_mail_ids": {"type": "one2many", "relation": "mail.mail"},
    },
}


class _FakeClient:
    def __init__(self) -> None:
        self.is_admin: bool | None = None
        self.admin_reason: str | None = None
        self.username = "u"
        self.uid = 1
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.written: list[tuple[str, list[int], dict[str, Any]]] = []

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        if model in _META:
            return _META[model]
        # Answer every key the payloads use, so a refusal can only come
        # from the model gate — never from an "unknown field" check.
        keys = {k for m, p in _QUEUE_PAYLOADS if m == model for k in p}
        meta: dict[str, dict[str, Any]] = {"id": {"type": "integer"}}
        for k in keys:
            meta[k] = {"type": "char"}
        if model == "mail.scheduled.message":
            meta["partner_ids"] = {"type": "many2many", "relation": "res.partner"}
        return meta

    def create(self, model: str, values: dict[str, Any]) -> int:
        self.created.append((model, values))
        return 42

    def write(self, model: str, ids: list[int], values: dict[str, Any]) -> bool:
        self.written.append((model, ids, values))
        return True


def _build(tmp_path: Path, *, external_comms: bool) -> tuple[Dispatcher, _FakeClient]:
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
        external_comms_enabled=external_comms,
    )
    creds = Credentials(instance_name=cfg.name, username="u", _api_key="k" * 10)
    real = OdooClient(cfg, credentials=creds)
    app_cfg = AppConfig(
        path=tmp_path / "config.toml",
        defaults=Defaults(),
        instances={cfg.name: cfg},
        audit_log_path=tmp_path / "audit.jsonl",
    )
    rl = RateLimiter()
    rl.configure(cfg.name, cfg.rate_limit_per_minute)
    fake = _FakeClient()
    rt = InstanceRuntime(config=cfg, client=real)
    rt.client = fake  # type: ignore[assignment]
    app = OdooMcpApp(
        config=app_cfg,
        audit=AuditLog(app_cfg.audit_log_path),
        prod_guard=ProdGuard(),
        rate_limiter=rl,
        instances={cfg.name: rt},
    )
    return Dispatcher(app), fake


def _call(disp: Dispatcher, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    contents = asyncio.run(disp.call(tool, args))
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


@pytest.mark.parametrize("external_comms", [False, True])
@pytest.mark.parametrize(("model", "values"), _QUEUE_PAYLOADS, ids=_QUEUE_MODELS)
def test_create_on_queue_model_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    values: dict[str, Any],
    external_comms: bool,
) -> None:
    """Refused with the comms opt-ins off (the bypass) AND on: even an
    operator who allows outbound messages gets them only through
    ``odoo_send_message``'s dry-run + recipient preview."""
    if external_comms:
        monkeypatch.setenv("ODOO_MCP_ENABLE_EXTERNAL_COMMS", "1")
    else:
        monkeypatch.delenv("ODOO_MCP_ENABLE_EXTERNAL_COMMS", raising=False)
    disp, fake = _build(tmp_path, external_comms=external_comms)
    payload = _call(
        disp,
        "odoo_create",
        {"instance": "dev", "model": model, "values": values, "dry_run": False},
    )
    assert payload["ok"] is False
    assert payload["error_code"] == "model_not_allowed"
    assert "read-only via the MCP" in payload["error"]
    assert fake.created == []


@pytest.mark.parametrize(("model", "values"), _QUEUE_PAYLOADS, ids=_QUEUE_MODELS)
def test_write_on_queue_model_refused(tmp_path: Path, model: str, values: dict[str, Any]) -> None:
    """``mail.mail`` delegates to ``mail.message``: a write here would
    rewrite a chatter message. A write can also re-queue a failed send."""
    disp, fake = _build(tmp_path, external_comms=False)
    payload = _call(
        disp,
        "odoo_write",
        {"instance": "dev", "model": model, "ids": [1], "values": values, "dry_run": False},
    )
    assert payload["ok"] is False
    assert payload["error_code"] == "model_not_allowed"
    assert fake.written == []


def test_queue_row_smuggled_through_x2many_refused(tmp_path: Path) -> None:
    """The relational-command pass applies the same write-blocklist to
    the comodel, so a (0, 0, vals) into a queue model is refused too."""
    disp, fake = _build(tmp_path, external_comms=False)
    payload = _call(
        disp,
        "odoo_write",
        {
            "instance": "dev",
            "model": "res.partner",
            "ids": [1],
            "values": {"x_mail_ids": [(0, 0, {"email_to": "a@example.com"})]},
            "dry_run": False,
        },
    )
    assert payload["ok"] is False
    assert payload["error_code"] == "model_not_allowed"
    assert fake.written == []


@pytest.mark.parametrize("model", _QUEUE_MODELS)
def test_queue_models_stay_readable(model: str) -> None:
    """Write-blocklisted, not denylisted: inspecting the queue (what is
    stuck, what failed) remains possible."""
    check_model(model, frozenset({ALLOWLIST_WILDCARD}))
    assert model in MODEL_WRITE_BLOCKLIST


@pytest.mark.parametrize("model", _QUEUE_MODELS)
def test_queue_model_names_are_real_odoo_models(model: str) -> None:
    """Pin the names against the generated Odoo reference so a typo can
    never leave a queue silently unblocked."""
    assert model in ODOO_STANDARD_MODELS
