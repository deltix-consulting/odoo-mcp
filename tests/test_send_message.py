"""Tests for the odoo_send_message tool.

The tool is double-gated (env var + per-instance config), runs through
the standard prod-guard pipeline (unlock + dry-run + token), and always
defaults to dry-run on both prod and dev. These tests pin each gate.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from mcp.types import ListToolsRequest

from odoo_mcp import server
from odoo_mcp.audit import AuditLog
from odoo_mcp.client import OdooClient
from odoo_mcp.config import AppConfig, Defaults, InstanceConfig
from odoo_mcp.credentials import Credentials
from odoo_mcp.dispatcher import Dispatcher, InstanceRuntime, OdooMcpApp
from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD
from odoo_mcp.security.limits import RateLimiter
from odoo_mcp.security.prod_guard import ProdGuard


class _FakeClient:
    def __init__(self) -> None:
        self.is_admin: bool | None = False
        self.admin_reason: str | None = None
        self.username = "u"
        self.uid = 1
        self.message_post_calls: list[dict[str, Any]] = []

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        return {"id": {"type": "integer"}, "name": {"type": "char"}}

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
        return 9999  # fake mail.message id


def _build(
    tmp_path: Path,
    *,
    production: bool = False,
    external_comms_enabled: bool = True,
) -> tuple[OdooMcpApp, _FakeClient]:
    cfg = InstanceConfig(
        name="dev",
        url="https://example.odoo.com",
        database="db",
        credentials_env_prefix="ODOO_MCP_DEV",
        production=production,
        timeout_seconds=30,
        max_records_default=50,
        max_records_hard_cap=500,
        rate_limit_per_minute=300,
        allow_self_signed=False,
        allowed_models=frozenset({ALLOWLIST_WILDCARD}),
        external_comms_enabled=external_comms_enabled,
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
    return app, fake


def _call(disp: Dispatcher, args: dict[str, Any]) -> dict[str, Any]:
    contents = asyncio.run(disp.call("odoo_send_message", args))
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


@pytest.fixture
def external_comms_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_MCP_ENABLE_EXTERNAL_COMMS", "1")


# ---------------------------------------------------------------------------
# Gate 1: env var
# ---------------------------------------------------------------------------


def test_refused_without_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ODOO_MCP_ENABLE_EXTERNAL_COMMS", raising=False)
    app, _ = _build(tmp_path)
    payload = _call(
        Dispatcher(app),
        {"instance": "dev", "model": "res.partner", "record_id": 1, "body": "hi"},
    )
    assert payload["ok"] is False
    assert "ODOO_MCP_ENABLE_EXTERNAL_COMMS" in payload["error"]


# ---------------------------------------------------------------------------
# Gate 2: per-instance flag
# ---------------------------------------------------------------------------


def test_refused_when_instance_flag_off(tmp_path: Path, external_comms_env: None) -> None:
    app, _ = _build(tmp_path, external_comms_enabled=False)
    payload = _call(
        Dispatcher(app),
        {"instance": "dev", "model": "res.partner", "record_id": 1, "body": "hi"},
    )
    assert payload["ok"] is False
    assert "external_comms_enabled" in payload["error"]


# ---------------------------------------------------------------------------
# Gate 3+4: dry-run default + token requirement (on dev too)
# ---------------------------------------------------------------------------


def test_dev_call_defaults_to_dry_run(tmp_path: Path, external_comms_env: None) -> None:
    """Even on dev, the first call returns a preview, never sends."""
    app, fake = _build(tmp_path, production=False)
    payload = _call(
        Dispatcher(app),
        {
            "instance": "dev",
            "model": "res.partner",
            "record_id": 7,
            "body": "<p>Test</p>",
            "subject": "hi",
            "partner_ids": [7],
        },
    )
    assert payload["ok"] is True
    assert payload["preview"] is True
    assert payload["body_preview"] == "<p>Test</p>"
    assert payload["partner_ids"] == [7]
    assert payload["would_send_email"] is True
    assert "confirmation_token" in payload
    # No actual send happened.
    assert fake.message_post_calls == []


def test_dev_commit_requires_token(tmp_path: Path, external_comms_env: None) -> None:
    app, fake = _build(tmp_path, production=False)
    # Step 1: dry run to get a token.
    preview = _call(
        Dispatcher(app),
        {"instance": "dev", "model": "res.partner", "record_id": 7, "body": "hi"},
    )
    token = preview["confirmation_token"]
    # Step 2: commit with dry_run=false + token.
    commit = _call(
        Dispatcher(app),
        {
            "instance": "dev",
            "model": "res.partner",
            "record_id": 7,
            "body": "hi",
            "dry_run": False,
            "confirmation_token": token,
        },
    )
    # On dev, _consume_token_on_prod is a no-op (production=False),
    # so the commit goes through. The send happens with the right
    # arguments.
    assert commit["ok"] is True
    assert commit["committed"] is True
    assert commit["message_id"] == 9999
    assert len(fake.message_post_calls) == 1
    call = fake.message_post_calls[0]
    assert call["model"] == "res.partner"
    assert call["record_id"] == 7
    assert call["body"] == "hi"


def test_prod_commit_requires_unlock_and_token(tmp_path: Path, external_comms_env: None) -> None:
    app, fake = _build(tmp_path, production=True)

    # Step 1: no unlock — refused.
    no_unlock = _call(
        Dispatcher(app),
        {"instance": "dev", "model": "res.partner", "record_id": 7, "body": "hi"},
    )
    assert no_unlock["ok"] is False
    assert "blocked" in no_unlock["error"].lower() or "unlock" in no_unlock["error"].lower()

    # Step 2: unlock prod.
    unlock = asyncio.run(Dispatcher(app).call("odoo_enable_prod_writes", {"instance": "dev"}))
    assert json.loads(unlock[0].text)["ok"] is True

    # Step 3: dry-run preview.
    preview = _call(
        Dispatcher(app),
        {"instance": "dev", "model": "res.partner", "record_id": 7, "body": "hi"},
    )
    assert preview["preview"] is True
    token = preview["confirmation_token"]

    # Step 4: real commit — no token provided.
    no_token = _call(
        Dispatcher(app),
        {
            "instance": "dev",
            "model": "res.partner",
            "record_id": 7,
            "body": "hi",
            "dry_run": False,
        },
    )
    assert no_token["ok"] is False
    assert "confirmation_token" in no_token["error"]

    # Step 5: real commit with token.
    commit = _call(
        Dispatcher(app),
        {
            "instance": "dev",
            "model": "res.partner",
            "record_id": 7,
            "body": "hi",
            "dry_run": False,
            "confirmation_token": token,
        },
    )
    assert commit["ok"] is True
    assert commit["committed"] is True
    assert len(fake.message_post_calls) == 1


# ---------------------------------------------------------------------------
# Validation: message_type and partner_ids
# ---------------------------------------------------------------------------


def test_invalid_message_type_rejected(tmp_path: Path, external_comms_env: None) -> None:
    app, _ = _build(tmp_path)
    payload = _call(
        Dispatcher(app),
        {
            "instance": "dev",
            "model": "res.partner",
            "record_id": 1,
            "body": "hi",
            "message_type": "broadcast",
        },
    )
    assert payload["ok"] is False
    assert "message_type" in payload["error"]


def test_partner_ids_must_be_integers(tmp_path: Path, external_comms_env: None) -> None:
    app, _ = _build(tmp_path)
    payload = _call(
        Dispatcher(app),
        {
            "instance": "dev",
            "model": "res.partner",
            "record_id": 1,
            "body": "hi",
            "partner_ids": ["not-an-int"],
        },
    )
    assert payload["ok"] is False
    assert "integer" in payload["error"].lower()


def test_log_note_does_not_promise_email(tmp_path: Path, external_comms_env: None) -> None:
    """message_type='notification' explicitly does NOT email anyone."""
    app, _ = _build(tmp_path)
    payload = _call(
        Dispatcher(app),
        {
            "instance": "dev",
            "model": "res.partner",
            "record_id": 1,
            "body": "internal note",
            "message_type": "notification",
            "partner_ids": [1, 2, 3],  # noise — still no email for notifications
        },
    )
    assert payload["preview"] is True
    assert payload["would_send_email"] is False


# ---------------------------------------------------------------------------
# Tool advertisement gating
# ---------------------------------------------------------------------------


def _advertised_tools(app: OdooMcpApp) -> set[str]:
    srv = server.build_server(app)
    handler = srv.request_handlers[ListToolsRequest]
    result = asyncio.run(handler(ListToolsRequest(method="tools/list")))
    return {t.name for t in result.root.tools}


def test_tool_hidden_when_env_var_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ODOO_MCP_ENABLE_EXTERNAL_COMMS", raising=False)
    app, _ = _build(tmp_path, external_comms_enabled=True)
    assert "odoo_send_message" not in _advertised_tools(app)


def test_tool_hidden_when_no_instance_opted_in(tmp_path: Path, external_comms_env: None) -> None:
    app, _ = _build(tmp_path, external_comms_enabled=False)
    assert "odoo_send_message" not in _advertised_tools(app)


def test_tool_visible_when_both_gates_open(tmp_path: Path, external_comms_env: None) -> None:
    app, _ = _build(tmp_path, external_comms_enabled=True)
    assert "odoo_send_message" in _advertised_tools(app)


# ---------------------------------------------------------------------------
# Read-only session still wins
# ---------------------------------------------------------------------------


def test_read_only_session_blocks_send(
    tmp_path: Path,
    external_comms_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ODOO_MCP_READ_ONLY", "1")
    app, _ = _build(tmp_path)
    payload = _call(
        Dispatcher(app),
        {"instance": "dev", "model": "res.partner", "record_id": 1, "body": "hi"},
    )
    assert payload["ok"] is False
    assert "read-only" in payload["error"].lower()
