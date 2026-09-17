"""The odoo_archive_or_delete dry run must identify the records it would remove.

``odoo_archive_or_delete`` is the only tool whose commit is irreversible.
Its dry-run preview used to report ``id_count`` alone — not even the ids —
so the human approving a permanent unlink could not tell WHICH records the
agent had selected. Every sibling already showed its payload
(``odoo_run_document_action`` -> ``record_ids`` + ``current_states``,
``odoo_send_message`` / ``odoo_log_note`` -> ``body_preview``).

These tests pin the identification so a future preview trim cannot quietly
take it back out, and pin the redaction / best-effort behaviour of the
``_peek_labels`` read that produces it.
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
from odoo_mcp.dispatcher import (
    _PREVIEW_LABEL_CAP,
    Dispatcher,
    InstanceRuntime,
    OdooMcpApp,
)
from odoo_mcp.errors import OdooMcpError
from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD
from odoo_mcp.security.limits import RateLimiter
from odoo_mcp.security.prod_guard import ProdGuard

# A model with a display_name and an active field, so both archive and
# delete reach the label read.
_FIELDS: dict[str, dict[str, Any]] = {
    "id": {"type": "integer"},
    "active": {"type": "boolean"},
    "display_name": {"type": "char"},
}


class _FakeClient:
    """Records read calls so tests can assert on the label peek."""

    def __init__(
        self,
        *,
        fields: dict[str, dict[str, Any]] | None = None,
        names: dict[int, str] | None = None,
        read_raises: bool = False,
    ) -> None:
        self._fields = fields if fields is not None else _FIELDS
        self._names = names or {}
        self._read_raises = read_raises
        self.read_calls: list[tuple[str, list[int], list[str]]] = []
        self.unlink_calls: list[tuple[str, list[int]]] = []
        self.write_calls: list[tuple[str, list[int], dict[str, Any]]] = []
        self.is_admin: bool | None = None
        self.admin_reason: str | None = None

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        return self._fields

    def read(self, model: str, ids: list[int], fields: list[str]) -> list[dict[str, Any]]:
        self.read_calls.append((model, list(ids), list(fields)))
        if self._read_raises:
            raise OdooMcpError("Odoo read failed")
        return [{"id": i, "display_name": self._names.get(i, f"Record {i}")} for i in ids]

    def write(self, model: str, ids: list[int], values: dict[str, Any]) -> bool:
        self.write_calls.append((model, ids, values))
        return True

    def unlink(self, model: str, ids: list[int]) -> bool:
        self.unlink_calls.append((model, ids))
        return True


def _build_app(
    tmp_path: Path,
    *,
    production: bool = False,
    fields: dict[str, dict[str, Any]] | None = None,
    names: dict[int, str] | None = None,
    read_raises: bool = False,
    sensitive_fields: dict[str, frozenset[str]] | None = None,
) -> tuple[OdooMcpApp, _FakeClient]:
    name = "prod" if production else "dev"
    inst_cfg = InstanceConfig(
        name=name,
        url="https://example.odoo.com",
        database="db",
        credentials_env_prefix=f"ODOO_MCP_{name.upper()}",
        production=production,
        timeout_seconds=30,
        max_records_default=50,
        max_records_hard_cap=500,
        rate_limit_per_minute=300,
        allow_self_signed=False,
        allowed_models=frozenset({ALLOWLIST_WILDCARD}),
        sensitive_fields=sensitive_fields or {},
    )
    creds = Credentials(instance_name=inst_cfg.name, username="u", _api_key="k" * 10)
    real_client = OdooClient(inst_cfg, credentials=creds)
    fake = _FakeClient(fields=fields, names=names, read_raises=read_raises)
    app_cfg = AppConfig(
        path=tmp_path / "config.toml",
        defaults=Defaults(),
        instances={inst_cfg.name: inst_cfg},
        audit_log_path=tmp_path / "audit.jsonl",
    )
    rate_limiter = RateLimiter()
    rate_limiter.configure(inst_cfg.name, inst_cfg.rate_limit_per_minute)
    rt = InstanceRuntime(config=inst_cfg, client=real_client)
    rt.client = fake  # type: ignore[assignment]
    app = OdooMcpApp(
        config=app_cfg,
        audit=AuditLog(app_cfg.audit_log_path),
        prod_guard=ProdGuard(),
        rate_limiter=rate_limiter,
        instances={inst_cfg.name: rt},
    )
    return app, fake


def _call(dispatcher: Dispatcher, args: dict[str, Any]) -> dict[str, Any]:
    contents = asyncio.run(dispatcher.call("odoo_archive_or_delete", args))
    payload: dict[str, Any] = json.loads(contents[0].text)
    return payload


# -- The identification itself -------------------------------------------------


def test_delete_dry_run_names_the_records_it_would_destroy(tmp_path: Path) -> None:
    """The headline: a permanent unlink preview must say WHICH records."""
    app, _ = _build_app(tmp_path, names={7: "Acme NV", 9: "Umbrella BV"})
    out = _call(
        Dispatcher(app),
        {
            "instance": "dev",
            "model": "res.partner",
            "mode": "delete",
            "ids": [7, 9],
            "dry_run": True,
        },
    )
    assert out["preview"] is True
    assert out["would_affect_records"] == [
        {"id": 7, "display_name": "Acme NV"},
        {"id": 9, "display_name": "Umbrella BV"},
    ]


def test_dry_run_echoes_the_ids_the_digest_binds(tmp_path: Path) -> None:
    """``ids`` are payload-bound, so the operator must get to see them.

    The commit result has always returned ``ids``; the preview returning
    only ``id_count`` made the approval step strictly less informative
    than its own outcome.
    """
    app, _ = _build_app(tmp_path)
    out = _call(
        Dispatcher(app),
        {
            "instance": "dev",
            "model": "res.partner",
            "mode": "delete",
            "ids": [4, 5, 6],
            "dry_run": True,
        },
    )
    assert out["ids"] == [4, 5, 6]
    assert out["id_count"] == 3


def test_archive_dry_run_also_identifies_records(tmp_path: Path) -> None:
    app, _ = _build_app(tmp_path, names={3: "Old Lead"})
    out = _call(
        Dispatcher(app),
        {"instance": "dev", "model": "crm.lead", "mode": "archive", "ids": [3], "dry_run": True},
    )
    assert out["would_affect_records"] == [{"id": 3, "display_name": "Old Lead"}]


def test_prod_delete_preview_identifies_records_before_the_token(tmp_path: Path) -> None:
    """The gate that actually matters: production, where the token is required."""
    app, _ = _build_app(tmp_path, production=True, names={11: "Real Customer"})
    app.prod_guard.unlock("prod", production=True)
    out = _call(
        Dispatcher(app),
        {
            "instance": "prod",
            "model": "res.partner",
            "mode": "delete",
            "ids": [11],
            "dry_run": True,
        },
    )
    assert out["confirmation_token"]
    assert out["would_affect_records"] == [{"id": 11, "display_name": "Real Customer"}]


# -- Best-effort / safety behaviour --------------------------------------------


def test_label_read_is_capped_and_flagged_as_truncated(tmp_path: Path) -> None:
    ids = list(range(1, _PREVIEW_LABEL_CAP + 11))
    app, fake = _build_app(tmp_path)
    out = _call(
        Dispatcher(app),
        {"instance": "dev", "model": "res.partner", "mode": "delete", "ids": ids, "dry_run": True},
    )
    assert len(out["would_affect_records"]) == _PREVIEW_LABEL_CAP
    assert out["would_affect_records_truncated"] is True
    # The true total is still reported.
    assert out["id_count"] == len(ids)
    # And we never asked Odoo for more than the cap.
    assert len(fake.read_calls[0][1]) == _PREVIEW_LABEL_CAP


def test_no_truncation_flag_when_every_record_is_labelled(tmp_path: Path) -> None:
    app, _ = _build_app(tmp_path)
    out = _call(
        Dispatcher(app),
        {
            "instance": "dev",
            "model": "res.partner",
            "mode": "delete",
            "ids": [1, 2],
            "dry_run": True,
        },
    )
    assert "would_affect_records_truncated" not in out


def test_model_without_display_name_still_previews(tmp_path: Path) -> None:
    """Key omitted, not set to [] — an empty list reads as 'records gone'."""
    app, fake = _build_app(
        tmp_path, fields={"id": {"type": "integer"}, "active": {"type": "boolean"}}
    )
    out = _call(
        Dispatcher(app),
        {"instance": "dev", "model": "some.model", "mode": "delete", "ids": [1], "dry_run": True},
    )
    assert out["preview"] is True
    assert "would_affect_records" not in out
    assert fake.read_calls == []  # no pointless RPC


def test_failed_label_read_does_not_break_the_preview(tmp_path: Path) -> None:
    """Never block an approval gate on a cosmetic read."""
    app, _ = _build_app(tmp_path, read_raises=True)
    out = _call(
        Dispatcher(app),
        {"instance": "dev", "model": "res.partner", "mode": "delete", "ids": [1], "dry_run": True},
    )
    assert out["preview"] is True
    assert out["confirmation_token"]
    assert "would_affect_records" not in out


def test_sensitive_display_name_is_redacted_but_id_survives(tmp_path: Path) -> None:
    """An operator-marked-sensitive label must not leak into the preview.

    The record is still identified by id, so the gate keeps working.
    """
    app, _ = _build_app(
        tmp_path,
        names={2: "Secret Person"},
        sensitive_fields={"res.partner": frozenset({"display_name"})},
    )
    out = _call(
        Dispatcher(app),
        {"instance": "dev", "model": "res.partner", "mode": "delete", "ids": [2], "dry_run": True},
    )
    assert out["would_affect_records"] == [{"id": 2}]


# -- The commit path is unchanged ----------------------------------------------


def test_commit_does_not_peek_labels(tmp_path: Path) -> None:
    """The label read belongs to the preview only — no extra RPC on commit."""
    app, fake = _build_app(tmp_path)
    _call(
        Dispatcher(app),
        {"instance": "dev", "model": "res.partner", "mode": "delete", "ids": [1], "dry_run": False},
    )
    assert fake.unlink_calls == [("res.partner", [1])]
    assert fake.read_calls == []
