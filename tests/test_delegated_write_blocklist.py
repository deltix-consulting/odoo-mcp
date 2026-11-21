"""Writes on a model that ``_inherits`` a blocked model are refused.

``website.page`` holds every ``ir.ui.view`` field (``arch``, ``inherit_id``,
``key``) through Odoo's delegation inheritance, and ``product.document`` /
``quotation.document`` hold every ``ir.attachment`` field (``public``,
``res_model``, ``datas``). Blocking only the parent's name left the parent
writable under the child's name. These tests pin that every write path on the
child is refused before any Odoo call, and that reads stay open.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from odoo_mcp._odoo_reference import ODOO_STANDARD_FIELDS, ODOO_STANDARD_MODELS
from odoo_mcp.audit import AuditLog
from odoo_mcp.client import OdooClient
from odoo_mcp.config import AppConfig, Defaults, InstanceConfig
from odoo_mcp.credentials import Credentials
from odoo_mcp.dispatcher import Dispatcher, InstanceRuntime, OdooMcpApp
from odoo_mcp.security.allowlist import (
    ALLOWLIST_WILDCARD,
    DELEGATED_WRITE_BLOCKLIST,
    MODEL_DENYLIST,
    MODEL_WRITE_BLOCKLIST,
    check_model,
    is_write_blocklisted,
)

_META: dict[str, dict[str, dict[str, Any]]] = {
    "website.page": {
        "id": {"type": "integer"},
        "name": {"type": "char"},
        "url": {"type": "char"},
        "arch": {"type": "text"},
        "inherit_id": {"type": "many2one", "relation": "ir.ui.view"},
        "view_id": {"type": "many2one", "relation": "ir.ui.view"},
    },
    "product.document": {
        "id": {"type": "integer"},
        "name": {"type": "char"},
        "public": {"type": "boolean"},
        "res_model": {"type": "char"},
        "ir_attachment_id": {"type": "many2one", "relation": "ir.attachment"},
    },
    "product.template": {
        "id": {"type": "integer"},
        "name": {"type": "char"},
        "product_document_ids": {"type": "one2many", "relation": "product.document"},
    },
}


class _FakeClient:
    def __init__(self) -> None:
        self.is_admin: bool | None = None
        self.admin_reason: str | None = None
        self.username = "u"
        self.uid = 1
        self.calls: list[tuple[str, str]] = []

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True, **_: Any) -> dict[str, Any]:
        return _META.get(model, {"id": {"type": "integer"}, "name": {"type": "char"}})

    def create(self, model: str, values: Any, **_: Any) -> int:
        self.calls.append(("create", model))
        return 42

    def write(self, model: str, ids: list[int], values: dict[str, Any], **_: Any) -> bool:
        self.calls.append(("write", model))
        return True

    def unlink(self, model: str, ids: list[int], **_: Any) -> bool:
        self.calls.append(("unlink", model))
        return True


def _build(tmp_path: Path) -> tuple[Dispatcher, _FakeClient]:
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
    app_cfg = AppConfig(
        path=tmp_path / "config.toml",
        defaults=Defaults(),
        instances={cfg.name: cfg},
        audit_log_path=tmp_path / "audit.jsonl",
    )
    from odoo_mcp.security.limits import RateLimiter
    from odoo_mcp.security.prod_guard import ProdGuard

    rl = RateLimiter()
    rl.configure(cfg.name, cfg.rate_limit_per_minute)
    fake = _FakeClient()
    rt = InstanceRuntime(config=cfg, client=OdooClient(cfg, credentials=creds))
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


def _assert_refused(payload: dict[str, Any], parent: str) -> None:
    assert payload["ok"] is False, payload
    assert payload["error_code"] == "model_not_allowed"
    assert parent in json.dumps(payload)


# --- every write path on a delegated child is refused ----------------------


@pytest.mark.parametrize("dry_run", [True, False])
def test_create_website_page_with_view_arch_refused(tmp_path: Path, dry_run: bool) -> None:
    disp, fake = _build(tmp_path)
    payload = _call(
        disp,
        "odoo_create",
        {
            "instance": "dev",
            "model": "website.page",
            "values": {
                "name": "x",
                "url": "/x",
                "inherit_id": 1,
                "arch": "<t t-name='x'><script>steal()</script></t>",
            },
            "dry_run": dry_run,
        },
    )
    _assert_refused(payload, "ir.ui.view")
    assert fake.calls == []


def test_write_product_document_public_refused(tmp_path: Path) -> None:
    disp, fake = _build(tmp_path)
    payload = _call(
        disp,
        "odoo_write",
        {
            "instance": "dev",
            "model": "product.document",
            "ids": [3],
            "values": {"public": True},
            "dry_run": False,
        },
    )
    _assert_refused(payload, "ir.attachment")
    assert fake.calls == []


def test_create_product_document_on_existing_attachment_refused(tmp_path: Path) -> None:
    # Odoo's create writes the delegated values onto the parent named by
    # ir_attachment_id: this would make attachment 99 public.
    disp, fake = _build(tmp_path)
    payload = _call(
        disp,
        "odoo_create",
        {
            "instance": "dev",
            "model": "product.document",
            "values": {"ir_attachment_id": 99, "public": True},
            "dry_run": False,
        },
    )
    _assert_refused(payload, "ir.attachment")
    assert fake.calls == []


@pytest.mark.parametrize("mode", ["archive", "delete"])
def test_archive_or_delete_quotation_document_refused(tmp_path: Path, mode: str) -> None:
    disp, fake = _build(tmp_path)
    payload = _call(
        disp,
        "odoo_archive_or_delete",
        {
            "instance": "dev",
            "model": "quotation.document",
            "ids": [1],
            "mode": mode,
            "dry_run": False,
        },
    )
    _assert_refused(payload, "ir.attachment")
    assert fake.calls == []


def test_nested_create_through_one2many_refused(tmp_path: Path) -> None:
    disp, fake = _build(tmp_path)
    payload = _call(
        disp,
        "odoo_write",
        {
            "instance": "dev",
            "model": "product.template",
            "ids": [1],
            "values": {"product_document_ids": [(0, 0, {"name": "x", "res_model": "hr.employee"})]},
            "dry_run": False,
        },
    )
    _assert_refused(payload, "ir.attachment")
    assert fake.calls == []


def test_unrelated_model_still_writable(tmp_path: Path) -> None:
    disp, fake = _build(tmp_path)
    payload = _call(
        disp,
        "odoo_write",
        {
            "instance": "dev",
            "model": "product.template",
            "ids": [1],
            "values": {"name": "Chair"},
            "dry_run": False,
        },
    )
    assert payload["ok"] is True, payload
    assert fake.calls == [("write", "product.template")]


# --- the map itself ---------------------------------------------------------


def test_every_parent_is_blocked_and_every_child_stays_readable() -> None:
    open_mode = frozenset({ALLOWLIST_WILDCARD})
    for child, parent in DELEGATED_WRITE_BLOCKLIST.items():
        assert parent in MODEL_DENYLIST or parent in MODEL_WRITE_BLOCKLIST, parent
        assert child not in MODEL_DENYLIST, child
        check_model(child, open_mode)  # reads stay open
        assert is_write_blocklisted(child)


def test_children_carry_the_parent_fields_in_the_reference() -> None:
    # The reference is generated from a real Odoo 18 registry; the delegated
    # fields that make each entry dangerous must be there.
    expected = {
        "website.page": {"arch", "view_id"},
        "website.controller.page": {"view_id"},
        "product.document": {"ir_attachment_id"},
        "quotation.document": {"ir_attachment_id"},
    }
    assert set(expected) == set(DELEGATED_WRITE_BLOCKLIST)
    for model, names in expected.items():
        assert model in ODOO_STANDARD_MODELS
        assert names <= ODOO_STANDARD_FIELDS[model], model


def test_write_blocklist_flag_helper_covers_both_sets() -> None:
    assert is_write_blocklisted("res.users")
    assert is_write_blocklisted("website.page")
    assert not is_write_blocklisted("res.partner")
