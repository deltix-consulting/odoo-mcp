"""Tests for ``odoo_create_attachment`` — bounded ir.attachment surface.

``ir.attachment`` itself remains denylisted (no ``search_read`` access,
no ``unlink``). This tool is the single permitted write path: create-
only, with allowlist + write-blocklist enforced on the ``res_model``
argument, decoded-size cap, filename sanitization, existence check on
the target record, and the full prod-guard preview/commit pipeline.

The tests below pin both ends of the contract:

- Happy paths: dry-run preview returns the token + previewed metadata,
  commit creates the row, payload digest binds bytes-identical content.
- Refusal paths: denylisted res_model, write-blocklisted res_model,
  filename with path separator, decoded size over the 25 MB cap,
  invalid base64, nonexistent target record. Each must fail BEFORE
  any ``ir.attachment`` write hits Odoo.
"""

from __future__ import annotations

import asyncio
import base64
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


class _AttachFake:
    """Records create/search_count calls and returns canned shapes.

    Defaults: target record exists; create returns id 999. Tests
    flip ``record_exists`` and ``create_should_fail`` to exercise
    refusal paths.
    """

    def __init__(
        self,
        *,
        record_exists: bool = True,
        create_id: int = 999,
        create_should_fail: bool = False,
    ) -> None:
        self.record_exists = record_exists
        self.create_id = create_id
        self.create_should_fail = create_should_fail
        self.create_calls: list[tuple[str, dict[str, Any]]] = []
        self.search_count_calls: list[tuple[str, list[Any]]] = []
        self.is_admin: bool | None = False
        self.admin_reason: str | None = None
        self.uid = 7
        self.username: str | None = "alice"

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        return {}

    def search_count(self, model: str, domain: list[Any]) -> int:
        self.search_count_calls.append((model, domain))
        return 1 if self.record_exists else 0

    def create(self, model: str, values: dict[str, Any]) -> int:
        self.create_calls.append((model, values))
        if self.create_should_fail:
            raise AssertionError("client.create must not be reached for this test")
        return self.create_id


def _instance_config(*, production: bool = False) -> InstanceConfig:
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
    )


def _build(tmp_path: Path, fake: _AttachFake, *, production: bool = False) -> OdooMcpApp:
    cfg = _instance_config(production=production)
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
    contents = asyncio.run(disp.call("odoo_create_attachment", args))
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


def _b64(content: bytes) -> str:
    return base64.b64encode(content).decode("ascii")


# ---------------------------------------------------------------------------
# Operation + tool registration
# ---------------------------------------------------------------------------


def test_operation_is_write_op() -> None:
    """Hard guarantee: a refactor that drops the new Operation into
    _READ_OPS would silently send attachment creates through the
    read-path (no prod-guard, no token). Pin it as write."""
    assert is_write(Operation.CREATE_ATTACHMENT)
    assert not is_read(Operation.CREATE_ATTACHMENT)


def test_tool_registered_in_build_tools() -> None:
    names = [t.name for t in build_tools()]
    assert "odoo_create_attachment" in names


# ---------------------------------------------------------------------------
# Dry-run preview
# ---------------------------------------------------------------------------


def test_dry_run_returns_preview_with_token_and_metadata(tmp_path: Path) -> None:
    fake = _AttachFake()
    app = _build(tmp_path, fake, production=True)
    app.prod_guard.unlock("prod", production=True)
    dispatcher = Dispatcher(app)
    payload_bytes = b"%PDF-1.7\n... fake invoice ..."
    out = _call(
        dispatcher,
        {
            "instance": "prod",
            "res_model": "account.move",
            "res_id": 123,
            "filename": "invoice.pdf",
            "datas_base64": _b64(payload_bytes),
            "mimetype": "application/pdf",
            "dry_run": True,
        },
    )
    assert out["preview"] is True
    assert out["res_model"] == "account.move"
    assert out["res_id"] == 123
    assert out["filename"] == "invoice.pdf"
    assert out["size_bytes"] == len(payload_bytes)
    assert out["mimetype"] == "application/pdf"
    assert out["confirmation_token"].startswith("conf_")
    # No ir.attachment row was created during the preview.
    assert fake.create_calls == []


def test_dry_run_strips_data_url_prefix(tmp_path: Path) -> None:
    """Some agents emit a data: URL prefix. We tolerate it; the digest
    binding must still work end-to-end on a roundtrip."""
    fake = _AttachFake()
    app = _build(tmp_path, fake)
    dispatcher = Dispatcher(app)
    raw = b"hello"
    encoded = "data:text/plain;base64," + _b64(raw)
    out = _call(
        dispatcher,
        {
            "instance": "dev",
            "res_model": "res.partner",
            "res_id": 1,
            "filename": "hello.txt",
            "datas_base64": encoded,
            "dry_run": True,
        },
    )
    assert out["preview"] is True
    assert out["size_bytes"] == len(raw)


# ---------------------------------------------------------------------------
# Commit path: dispatcher → client.create("ir.attachment", ...)
# ---------------------------------------------------------------------------


def test_commit_creates_ir_attachment_row(tmp_path: Path) -> None:
    """End-to-end: dry-run gets a token, commit consumes it and the
    dispatcher calls client.create with model=ir.attachment carrying
    the validated payload."""
    fake = _AttachFake(create_id=4242)
    app = _build(tmp_path, fake, production=True)
    app.prod_guard.unlock("prod", production=True)
    dispatcher = Dispatcher(app)
    content = b"hello world"

    preview = _call(
        dispatcher,
        {
            "instance": "prod",
            "res_model": "res.partner",
            "res_id": 1,
            "filename": "hello.txt",
            "datas_base64": _b64(content),
            "dry_run": True,
        },
    )
    token = preview["confirmation_token"]
    result = _call(
        dispatcher,
        {
            "instance": "prod",
            "res_model": "res.partner",
            "res_id": 1,
            "filename": "hello.txt",
            "datas_base64": _b64(content),
            "dry_run": False,
            "confirmation_token": token,
        },
    )
    assert result["committed"] is True
    assert result["attachment_id"] == 4242
    # Exactly one create call, on ir.attachment, with our payload.
    assert len(fake.create_calls) == 1
    model, values = fake.create_calls[0]
    assert model == "ir.attachment"
    assert values["name"] == "hello.txt"
    assert values["res_model"] == "res.partner"
    assert values["res_id"] == 1
    # datas is the original base64 string (Odoo decodes itself).
    assert base64.b64decode(values["datas"]) == content


def test_token_rejects_content_swap(tmp_path: Path) -> None:
    """Payload-digest contract: previewing a small placeholder cannot
    commit a different (larger, malicious) file with the same token.
    This is the v0.18.0 token-binding fix applied to attachments."""
    fake = _AttachFake()
    app = _build(tmp_path, fake, production=True)
    app.prod_guard.unlock("prod", production=True)
    dispatcher = Dispatcher(app)

    preview = _call(
        dispatcher,
        {
            "instance": "prod",
            "res_model": "res.partner",
            "res_id": 1,
            "filename": "placeholder.txt",
            "datas_base64": _b64(b"tiny"),
            "dry_run": True,
        },
    )
    token = preview["confirmation_token"]
    swapped = _call(
        dispatcher,
        {
            "instance": "prod",
            "res_model": "res.partner",
            "res_id": 1,
            "filename": "placeholder.txt",
            "datas_base64": _b64(b"completely different content x" * 100),
            "dry_run": False,
            "confirmation_token": token,
        },
    )
    assert swapped["ok"] is False
    assert "different payload" in swapped["error"]
    assert fake.create_calls == []


# ---------------------------------------------------------------------------
# Refusal paths — must fail BEFORE the ir.attachment write
# ---------------------------------------------------------------------------


def test_refuses_denylisted_res_model(tmp_path: Path) -> None:
    """Attaching to a denylisted model (e.g. ``ir.model``) must be
    refused by the standard check_model pipeline. This guarantees
    the user-facing ``res_model`` flows through the same security
    envelope as every other write tool — there's no special back
    door created by the attachment path."""
    fake = _AttachFake()
    app = _build(tmp_path, fake)
    dispatcher = Dispatcher(app)
    payload = _call(
        dispatcher,
        {
            "instance": "dev",
            "res_model": "ir.model",
            "res_id": 1,
            "filename": "x.txt",
            "datas_base64": _b64(b"x"),
        },
    )
    assert payload["ok"] is False
    assert "denylist" in payload["error"].lower() or "blocked" in payload["error"].lower()
    assert fake.create_calls == []


def test_refuses_write_blocklisted_res_model(tmp_path: Path) -> None:
    """Adding a file to a write-blocklisted model (e.g. ``res.users``)
    is semantically a write on that user. The write-blocklist must
    cover the attachment path too — pinned here so a refactor that
    forgets ``_refuse_write_blocklisted`` is loud."""
    fake = _AttachFake()
    app = _build(tmp_path, fake)
    dispatcher = Dispatcher(app)
    payload = _call(
        dispatcher,
        {
            "instance": "dev",
            "res_model": "res.users",
            "res_id": 1,
            "filename": "x.txt",
            "datas_base64": _b64(b"x"),
        },
    )
    assert payload["ok"] is False
    assert "read-only" in payload["error"].lower() or "blocklist" in payload["error"].lower()
    assert fake.create_calls == []


def test_refuses_filename_with_path_separator(tmp_path: Path) -> None:
    fake = _AttachFake()
    app = _build(tmp_path, fake)
    dispatcher = Dispatcher(app)
    payload = _call(
        dispatcher,
        {
            "instance": "dev",
            "res_model": "res.partner",
            "res_id": 1,
            "filename": "../etc/passwd",
            "datas_base64": _b64(b"x"),
        },
    )
    assert payload["ok"] is False
    assert "separator" in payload["error"].lower() or "filename" in payload["error"].lower()
    assert fake.create_calls == []


def test_refuses_invalid_base64(tmp_path: Path) -> None:
    fake = _AttachFake()
    app = _build(tmp_path, fake)
    dispatcher = Dispatcher(app)
    payload = _call(
        dispatcher,
        {
            "instance": "dev",
            "res_model": "res.partner",
            "res_id": 1,
            "filename": "x.txt",
            "datas_base64": "not!valid!!!base64@@",
        },
    )
    assert payload["ok"] is False
    assert "base64" in payload["error"].lower()
    assert fake.create_calls == []


def test_refuses_over_size_cap(tmp_path: Path) -> None:
    """26 MB > 25 MB cap → refused before any Odoo round-trip."""
    fake = _AttachFake()
    app = _build(tmp_path, fake)
    dispatcher = Dispatcher(app)
    too_big = b"A" * (26 * 1024 * 1024)
    payload = _call(
        dispatcher,
        {
            "instance": "dev",
            "res_model": "res.partner",
            "res_id": 1,
            "filename": "huge.bin",
            "datas_base64": _b64(too_big),
        },
    )
    assert payload["ok"] is False
    assert "cap" in payload["error"].lower() or "byte" in payload["error"].lower()
    assert fake.create_calls == []


def test_refuses_nonexistent_target_record(tmp_path: Path) -> None:
    """Orphan attachments are refused: search_count returns 0 → error.
    Without this, a typo'd res_id silently creates a dangling row that
    sidesteps Odoo's record-rules."""
    fake = _AttachFake(record_exists=False)
    app = _build(tmp_path, fake)
    dispatcher = Dispatcher(app)
    payload = _call(
        dispatcher,
        {
            "instance": "dev",
            "res_model": "res.partner",
            "res_id": 999_999,
            "filename": "x.txt",
            "datas_base64": _b64(b"x"),
        },
    )
    assert payload["ok"] is False
    assert "does not exist" in payload["error"] or "orphan" in payload["error"].lower()
    assert fake.create_calls == []


def test_ir_attachment_is_not_user_visible_via_search_read(tmp_path: Path) -> None:
    """Defense-in-depth: ``ir.attachment`` itself MUST stay on the
    denylist so the agent cannot exfiltrate arbitrary attachments
    via odoo_search_read. The new write path is the only exception."""
    from odoo_mcp.security.allowlist import MODEL_DENYLIST

    assert "ir.attachment" in MODEL_DENYLIST
