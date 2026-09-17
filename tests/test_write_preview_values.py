"""Tests for the odoo_write / odoo_create dry-run value preview.

A write dry run exists so a human can approve the commit. ``write``
replaces a field, it never appends, so approving it means approving the
destruction of whatever those fields hold today. These tests pin that the
preview shows both halves of that trade — ``would_set_values`` (what goes
in) and ``current_values`` (what it overwrites) — and that the read-back
does not become a hole around field redaction.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest

from odoo_mcp.audit import AuditLog
from odoo_mcp.client import OdooClient
from odoo_mcp.config import AppConfig, Defaults, InstanceConfig
from odoo_mcp.credentials import Credentials
from odoo_mcp.dispatcher import (
    _PREVIEW_LIST_CAP,
    _PREVIEW_RECORD_CAP,
    _PREVIEW_STR_CAP,
    Dispatcher,
    InstanceRuntime,
    OdooMcpApp,
    _truncate_preview,
)
from odoo_mcp.errors import OdooMcpError
from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD
from odoo_mcp.security.limits import RateLimiter
from odoo_mcp.security.prod_guard import ProdGuard


class _FakeClient:
    """Minimal Odoo stand-in with a settable stored-record table."""

    def __init__(
        self,
        *,
        stored: dict[int, dict[str, Any]] | None = None,
        fields: dict[str, dict[str, Any]] | None = None,
        read_raises: bool = False,
    ) -> None:
        self.is_admin: bool | None = False
        self.admin_reason: str | None = None
        self.username = "u"
        self.uid = 1
        self._stored = stored or {}
        self._fields = fields or {
            "id": {"type": "integer"},
            "description": {"type": "text"},
            "name": {"type": "char"},
        }
        self._read_raises = read_raises
        self.read_calls: list[tuple[str, list[int], list[str]]] = []
        self.write_calls: list[tuple[str, list[int], dict[str, Any]]] = []

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        return self._fields

    def read(self, model: str, ids: list[int], fields: list[str]) -> list[dict[str, Any]]:
        self.read_calls.append((model, list(ids), list(fields)))
        if self._read_raises:
            raise OdooMcpError("read blew up")
        out = []
        for i in ids:
            rec = self._stored.get(i, {})
            out.append({f: rec.get(f) for f in fields if f != "id"} | {"id": i})
        return out

    def write(self, model: str, ids: list[int], values: dict[str, Any]) -> bool:
        self.write_calls.append((model, list(ids), dict(values)))
        return True

    def create(self, model: str, values: dict[str, Any]) -> int:
        return 1


def _build(tmp_path: Path, client: _FakeClient, *, production: bool = True) -> OdooMcpApp:
    cfg = InstanceConfig(
        name="prod",
        url="https://example.odoo.com",
        database="db",
        credentials_env_prefix="ODOO_MCP_PROD",
        production=production,
        timeout_seconds=30,
        max_records_default=50,
        max_records_hard_cap=500,
        rate_limit_per_minute=300,
        allow_self_signed=False,
        allowed_models=frozenset({ALLOWLIST_WILDCARD}),
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
    rt.client = client  # type: ignore[assignment]
    app = OdooMcpApp(
        config=app_cfg,
        audit=AuditLog(app_cfg.audit_log_path),
        prod_guard=ProdGuard(),
        rate_limiter=rl,
        instances={cfg.name: rt},
    )
    if production:
        # Prod writes are blocked until explicitly unlocked; these tests are
        # about what the dry run *shows*, not about the unlock gate itself.
        app.prod_guard.unlock(cfg.name, production=True)
    return app


def _call(app: OdooMcpApp, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    contents = asyncio.run(Dispatcher(app).call(tool, args))
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


def _preview_write(app: OdooMcpApp, **overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "instance": "prod",
        "model": "crm.lead",
        "ids": [42],
        "values": {"description": "new note"},
        "dry_run": True,
    }
    args.update(overrides)
    return _call(app, "odoo_write", args)


def test_write_preview_shows_the_content_it_would_destroy(tmp_path: Path) -> None:
    client = _FakeClient(stored={42: {"description": "IMPORTANT: signed 2024 contract terms"}})
    app = _build(tmp_path, client)

    preview = _preview_write(app)

    assert preview["preview"] is True
    # The value going in...
    assert preview["would_set_values"] == {"description": "new note"}
    # ...and the value it replaces.
    assert preview["current_values"] == [
        {"id": 42, "description": "IMPORTANT: signed 2024 contract terms"}
    ]
    # Field names alone are not enough, but must not regress either.
    assert preview["would_update_fields"] == ["description"]


def test_write_preview_reads_back_only_the_fields_being_written(tmp_path: Path) -> None:
    client = _FakeClient(stored={42: {"description": "old", "name": "Acme"}})
    app = _build(tmp_path, client)

    preview = _preview_write(app)

    assert client.read_calls == [("crm.lead", [42], ["id", "description"])]
    assert preview["current_values"][0].keys() == {"id", "description"}


def test_write_preview_does_not_read_back_a_redacted_field(tmp_path: Path) -> None:
    """A default-hidden field is writable but must not be readable here.

    ``validate_write_values`` deliberately permits writing a default-hidden
    field while ``odoo_read`` requires ``allow_sensitive_fields`` to see it.
    The write preview must not become the hole that reads it back for free.

    ``res.partner.comment`` is the sharpest case: it is the partner Notes
    field — exactly the "internal notes" a careless write would destroy —
    and it is default-hidden, so the peek has to stay silent about it even
    though showing it would be the most useful thing here.
    """
    client = _FakeClient(
        stored={42: {"comment": "internal: do not call before Q3", "vat": "BE0123456789"}},
        fields={
            "id": {"type": "integer"},
            "comment": {"type": "text"},
            "vat": {"type": "char"},
        },
    )
    app = _build(tmp_path, client)

    preview = _preview_write(
        app,
        model="res.partner",
        values={"comment": "overwritten", "vat": "BE9999999999"},
    )

    serialized = repr(preview.get("current_values") or [])
    assert "do not call before Q3" not in serialized
    assert "BE0123456789" not in serialized
    # The caller's own input is still echoed — that is not an Odoo read.
    assert preview["would_set_values"] == {"comment": "overwritten", "vat": "BE9999999999"}


def test_write_preview_omits_current_values_when_the_read_fails(tmp_path: Path) -> None:
    """Best-effort: an omitted key, never a misleading empty list.

    ``[]`` would read as "these records do not exist" — a materially
    different claim from "we could not check".
    """
    client = _FakeClient(read_raises=True)
    app = _build(tmp_path, client)

    preview = _preview_write(app)

    assert "current_values" not in preview
    # The preview is still usable: token and intended values survive.
    assert preview["confirmation_token"]
    assert preview["would_set_values"] == {"description": "new note"}


def test_write_preview_caps_the_records_it_reads_back(tmp_path: Path) -> None:
    ids = list(range(1, 21))
    client = _FakeClient(stored={i: {"description": f"old {i}"} for i in ids})
    app = _build(tmp_path, client)

    preview = _preview_write(app, ids=ids)

    assert len(preview["current_values"]) == _PREVIEW_RECORD_CAP
    assert preview["current_values_truncated"] is True
    assert preview["id_count"] == 20


def test_write_preview_does_not_flag_truncation_when_all_records_fit(tmp_path: Path) -> None:
    client = _FakeClient(stored={1: {"description": "a"}, 2: {"description": "b"}})
    app = _build(tmp_path, client)

    preview = _preview_write(app, ids=[1, 2])

    assert len(preview["current_values"]) == 2
    assert "current_values_truncated" not in preview


def test_write_preview_truncates_a_huge_stored_value(tmp_path: Path) -> None:
    client = _FakeClient(stored={42: {"description": "x" * 5000}})
    app = _build(tmp_path, client)

    preview = _preview_write(app)

    shown = preview["current_values"][0]["description"]
    assert shown.endswith("...[truncated]")
    assert len(shown) < 5000


def test_create_preview_shows_the_values_it_would_write(tmp_path: Path) -> None:
    client = _FakeClient()
    app = _build(tmp_path, client)

    preview = _call(
        app,
        "odoo_create",
        {
            "instance": "prod",
            "model": "res.partner",
            "values": {"name": "Acme", "description": "hello"},
            "dry_run": True,
        },
    )

    assert preview["would_set_values"] == {"description": "hello", "name": "Acme"}
    assert preview["would_write_fields"] == ["description", "name"]
    # A create has nothing to overwrite.
    assert "current_values" not in preview


def test_commit_path_is_unchanged_by_the_preview(tmp_path: Path) -> None:
    """The peek must not leak into what actually gets written."""
    client = _FakeClient(stored={42: {"description": "old"}})
    app = _build(tmp_path, client)

    preview = _preview_write(app)
    _call(
        app,
        "odoo_write",
        {
            "instance": "prod",
            "model": "crm.lead",
            "ids": [42],
            "values": {"description": "new note"},
            "dry_run": False,
            "confirmation_token": preview["confirmation_token"],
        },
    )

    assert client.write_calls == [("crm.lead", [42], {"description": "new note"})]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("short", "short"),
        (7, 7),
        (None, None),
        (True, True),
        ([1, 2, 3], [1, 2, 3]),
        ({"a": "b"}, {"a": "b"}),
    ],
)
def test_truncate_preview_passes_small_values_through(value: Any, expected: Any) -> None:
    assert _truncate_preview(value) == expected


def test_truncate_preview_marks_long_strings() -> None:
    out = _truncate_preview("y" * (_PREVIEW_STR_CAP + 1))
    assert out == "y" * _PREVIEW_STR_CAP + "...[truncated]"


def test_truncate_preview_marks_long_lists_with_the_real_total() -> None:
    out = _truncate_preview(list(range(_PREVIEW_LIST_CAP + 5)))
    assert len(out) == _PREVIEW_LIST_CAP + 1
    assert re.fullmatch(rf"\.\.\.\[truncated, {_PREVIEW_LIST_CAP + 5} items total\]", out[-1])


def test_truncate_preview_recurses_into_m2m_command_tuples() -> None:
    out = _truncate_preview({"tag_ids": [[6, 0, list(range(_PREVIEW_LIST_CAP + 3))]]})
    inner = out["tag_ids"][0][2]
    assert len(inner) == _PREVIEW_LIST_CAP + 1
    assert "truncated" in inner[-1]


def test_help_gotchas_warn_that_write_replaces_rather_than_appends() -> None:
    from odoo_mcp.dispatcher import _HELP_GOTCHAS

    joined = " ".join(_HELP_GOTCHAS).lower()
    assert "odoo_write replaces" in joined
    assert "odoo_log_note" in joined
