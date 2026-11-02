"""A message, log note or attachment on a record Odoo cannot find is refused up front.

Odoo's ``message_post`` does not check the id. It fails late, on whatever
first reads the record: ``display_name`` on 16.0-18.0, the company field or
``mail.message``'s document access check on 19.0 (an AccessError, which reads
like a permission problem). So the preview approved a post on a stale id, the
commit spent the token, and only then did Odoo refuse.

``odoo_create_attachment`` already checked the target, but with Odoo's
implicit ``active = True`` filter, so an archived record was refused as
"does not exist". All three tools now share one check that counts archived
records as existing.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import pytest

from odoo_mcp.audit import AuditLog
from odoo_mcp.client import OdooClient
from odoo_mcp.config import AppConfig, Defaults, InstanceConfig
from odoo_mcp.credentials import Credentials
from odoo_mcp.dispatcher import Dispatcher, InstanceRuntime, OdooMcpApp
from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD
from odoo_mcp.security.limits import RateLimiter
from odoo_mcp.security.prod_guard import ProdGuard


class _Fake:
    """Holds one set of existing ids; archived ids are found only when the
    domain names ``active`` (as Odoo's implicit filter would hide them)."""

    def __init__(
        self, *, existing: set[int], archived: set[int] = frozenset(), has_active: bool = True
    ) -> None:  # type: ignore[assignment]
        self.existing = set(existing)
        self.archived = set(archived)
        self.has_active = has_active
        self.is_admin: bool | None = False
        self.admin_reason: str | None = None
        self.username = "u"
        self.uid = 7
        self.search_count_calls: list[tuple[str, list[Any]]] = []
        self.message_post_calls: list[int] = []
        self.create_calls: list[tuple[str, dict[str, Any]]] = []

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(
        self, model: str, *, use_cache: bool = True, **_: Any
    ) -> dict[str, dict[str, Any]]:
        meta: dict[str, dict[str, Any]] = {"id": {"type": "integer"}, "name": {"type": "char"}}
        if self.has_active:
            meta["active"] = {"type": "boolean"}
        return meta

    def search_count(self, model: str, domain: list[Any], **_: Any) -> int:
        self.search_count_calls.append((model, list(domain)))
        (_f, _op, rid) = domain[0]
        names_active = any(isinstance(leaf, tuple) and leaf[0] == "active" for leaf in domain)
        if rid in self.existing:
            return 1
        if rid in self.archived and names_active:
            return 1
        return 0

    def message_post(self, model: str, record_id: int, body: str, **_: Any) -> int:
        self.message_post_calls.append(record_id)
        return 4242

    def create(self, model: str, values: dict[str, Any], **_: Any) -> int:
        self.create_calls.append((model, values))
        return 999


def _build(tmp_path: Path, fake: _Fake, *, production: bool = False) -> OdooMcpApp:
    cfg = InstanceConfig(
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
        external_comms_enabled=True,
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
    rt = InstanceRuntime(config=cfg, client=real)
    rt.client = fake  # type: ignore[assignment]
    return OdooMcpApp(
        config=app_cfg,
        audit=AuditLog(app_cfg.audit_log_path),
        prod_guard=ProdGuard(),
        rate_limiter=rl,
        instances={cfg.name: rt},
    )


def _call(app: OdooMcpApp, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    contents = asyncio.run(Dispatcher(app).call(tool, args))
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


@pytest.fixture
def external_comms_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_MCP_ENABLE_EXTERNAL_COMMS", "1")


def _message_args(tool: str, record_id: int, **extra: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "instance": "dev",
        "model": "res.partner",
        "record_id": record_id,
        "body": "hi",
    }
    args.update(extra)
    return args


_MESSAGE_TOOLS = ("odoo_send_message", "odoo_log_note")


@pytest.mark.parametrize("tool", _MESSAGE_TOOLS)
def test_preview_refuses_a_missing_record(
    tmp_path: Path, external_comms_env: None, tool: str
) -> None:
    fake = _Fake(existing={1})
    app = _build(tmp_path, fake)
    payload = _call(app, tool, _message_args(tool, 99_999))
    assert payload["ok"] is False
    assert "res.partner(99999) does not exist" in payload["error"]
    assert "confirmation_token" not in payload
    assert fake.message_post_calls == []


@pytest.mark.parametrize("tool", _MESSAGE_TOOLS)
def test_dev_commit_without_preview_refuses_a_missing_record(
    tmp_path: Path, external_comms_env: None, tool: str
) -> None:
    """The check sits before the dry-run / commit split, so a dev commit
    that skips the preview is covered too."""
    fake = _Fake(existing={1})
    app = _build(tmp_path, fake)
    payload = _call(app, tool, _message_args(tool, 99_999, dry_run=False))
    assert payload["ok"] is False
    assert "does not exist" in payload["error"]
    assert fake.message_post_calls == []


@pytest.mark.parametrize("tool", _MESSAGE_TOOLS)
def test_an_archived_record_is_found(tmp_path: Path, external_comms_env: None, tool: str) -> None:
    fake = _Fake(existing=set(), archived={5})
    app = _build(tmp_path, fake)
    payload = _call(app, tool, _message_args(tool, 5, dry_run=False))
    assert payload.get("committed") is True, payload
    assert fake.message_post_calls == [5]
    assert fake.search_count_calls == [
        ("res.partner", [("id", "=", 5), ("active", "in", [True, False])])
    ]


@pytest.mark.parametrize("tool", _MESSAGE_TOOLS)
def test_model_without_active_sends_only_the_id_leaf(
    tmp_path: Path, external_comms_env: None, tool: str
) -> None:
    """Naming ``active`` on a model without it would make Odoo raise."""
    fake = _Fake(existing={3}, has_active=False)
    app = _build(tmp_path, fake)
    payload = _call(app, tool, _message_args(tool, 3, dry_run=True))
    assert payload.get("preview") is True, payload
    assert fake.search_count_calls == [("res.partner", [("id", "=", 3)])]


@pytest.mark.parametrize("tool", _MESSAGE_TOOLS)
def test_prod_commit_does_not_spend_the_token_on_a_missing_record(
    tmp_path: Path, external_comms_env: None, tool: str
) -> None:
    """Record deleted between preview and commit: the commit is refused
    before the token is consumed and before Odoo is called."""
    fake = _Fake(existing={8})
    app = _build(tmp_path, fake, production=True)
    app.prod_guard.unlock("prod", production=True)
    args = _message_args(tool, 8, instance="prod")
    preview = _call(app, tool, args)
    assert preview.get("preview") is True, preview
    fake.existing.clear()
    commit = _call(
        app, tool, {**args, "dry_run": False, "confirmation_token": preview["confirmation_token"]}
    )
    assert commit["ok"] is False
    assert "does not exist" in commit["error"]
    assert fake.message_post_calls == []


def test_attachment_on_an_archived_record_is_allowed(tmp_path: Path) -> None:
    fake = _Fake(existing=set(), archived={5})
    app = _build(tmp_path, fake)
    payload = _call(
        app,
        "odoo_create_attachment",
        {
            "instance": "dev",
            "res_model": "res.partner",
            "res_id": 5,
            "filename": "x.txt",
            "datas_base64": base64.b64encode(b"x").decode("ascii"),
        },
    )
    assert payload["ok"] is True, payload
    assert len(fake.create_calls) == 1
    assert fake.search_count_calls == [
        ("res.partner", [("id", "=", 5), ("active", "in", [True, False])])
    ]


def test_attachment_on_a_missing_record_is_still_refused(tmp_path: Path) -> None:
    fake = _Fake(existing=set())
    app = _build(tmp_path, fake)
    payload = _call(
        app,
        "odoo_create_attachment",
        {
            "instance": "dev",
            "res_model": "res.partner",
            "res_id": 99_999,
            "filename": "x.txt",
            "datas_base64": base64.b64encode(b"x").decode("ascii"),
            "dry_run": False,
        },
    )
    assert payload["ok"] is False
    assert "does not exist" in payload["error"]
    assert fake.create_calls == []
