"""Token-budget tests for v0.11.0 payload reductions.

Each test mocks the OdooClient with a realistic-sized payload and asserts
that the dispatcher's response stays within a token budget. These are the
guard-rails that prove the five reductions actually shrink the wire format
and prevent regressions.

Budgets are stated in characters (1 char ~= 0.25 tokens for JSON), which is
deterministic and doesn't depend on any tokenizer.
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
from odoo_mcp.errors import OdooMcpError
from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD
from odoo_mcp.security.limits import RateLimiter
from odoo_mcp.security.prod_guard import ProdGuard

# ---------------------------------------------------------------------------
# Fake client + app builder
# ---------------------------------------------------------------------------


def _big_field(name: str) -> dict[str, Any]:
    """Return a realistic Odoo fields_get entry for ``name`` with paragraph help."""
    return {
        "type": "char",
        "string": f"{name.replace('_', ' ').title()}",
        "required": False,
        "readonly": False,
        "help": (
            f"This is the {name} field on the model. It carries the data needed "
            f"for the {name} aspect of the record and is used by various business "
            f"rules across CRM, Sales, Accounting, and Inventory. Updating it may "
            f"trigger automated actions on related records — see the Odoo docs "
            f"for {name} for the full list of side effects."
        ),
        "relation": "",
    }


def _big_fields_get(n: int) -> dict[str, dict[str, Any]]:
    """Build a realistic fields_get with ``n`` fields."""
    out: dict[str, dict[str, Any]] = {"id": {"type": "integer", "string": "ID"}}
    for i in range(n - 1):
        out[f"field_{i:03d}"] = _big_field(f"field_{i:03d}")
    return out


class _FakeClient:
    def __init__(
        self,
        *,
        fields: dict[str, dict[str, Any]] | None = None,
        records: list[dict[str, Any]] | None = None,
        groups: list[dict[str, Any]] | None = None,
    ) -> None:
        self._fields = fields if fields is not None else {"id": {"type": "integer"}}
        self._records = records if records is not None else []
        self._groups = groups if groups is not None else []
        self.is_admin: bool | None = None
        self.admin_reason: str | None = None

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        return self._fields

    def search_read(
        self,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int,
        offset: int,
        order: str | None,
    ) -> list[dict[str, Any]]:
        return [dict(r) for r in self._records]

    def read(self, model: str, ids: list[int], fields: list[str]) -> list[dict[str, Any]]:
        return [dict(r) for r in self._records]

    def read_group(
        self,
        model: str,
        domain: list[Any],
        fields: list[str],
        groupby: list[str],
        *,
        limit: int,
        offset: int,
        orderby: str | None,
        lazy: bool,
    ) -> list[dict[str, Any]]:
        return [dict(g) for g in self._groups]


def _instance_config() -> InstanceConfig:
    return InstanceConfig(
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


def _build_app(tmp_path: Path, fake: _FakeClient) -> OdooMcpApp:
    cfg = _instance_config()
    creds = Credentials(instance_name=cfg.name, username="u", _api_key="k" * 10)
    real_client = OdooClient(cfg, credentials=creds)
    app_cfg = AppConfig(
        path=tmp_path / "config.toml",
        defaults=Defaults(),
        instances={cfg.name: cfg},
        audit_log_path=tmp_path / "audit.jsonl",
    )
    audit = AuditLog(app_cfg.audit_log_path)
    rate_limiter = RateLimiter()
    rate_limiter.configure(cfg.name, cfg.rate_limit_per_minute)
    rt = InstanceRuntime(config=cfg, client=real_client)
    rt.client = fake  # type: ignore[assignment]
    return OdooMcpApp(
        config=app_cfg,
        audit=audit,
        prod_guard=ProdGuard(),
        rate_limiter=rate_limiter,
        instances={cfg.name: rt},
    )


def _call(dispatcher: Dispatcher, name: str, args: dict[str, Any]) -> tuple[dict[str, Any], int]:
    contents = asyncio.run(dispatcher.call(name, args))
    text = contents[0].text
    return json.loads(text), len(text)


# ---------------------------------------------------------------------------
# 1. odoo_describe_model — minimal mode by default
# ---------------------------------------------------------------------------


def test_describe_model_default_significantly_smaller_than_verbose(tmp_path: Path) -> None:
    """Default mode on a 280-field model must be <=20% of verbose size.

    Hard budget: under 16k chars on the synthetic 280-field schema (verbose
    mode is ~120k for the same input). The pre-v0.11.0 default already
    dropped a few keys but still included `help` paragraphs per field.
    """
    fake = _FakeClient(fields=_big_fields_get(280))
    app = _build_app(tmp_path, fake)
    dispatcher = Dispatcher(app)

    payload, size = _call(
        dispatcher, "odoo_describe_model", {"instance": "dev", "model": "res.partner"}
    )
    assert payload["ok"] is True
    assert size < 16_000, f"default describe payload too large: {size} chars"

    # Sanity: each field has only the minimal keys we promised.
    sample = next(iter(payload["fields"].values()))
    allowed = {"type", "string", "required", "_sensitive"}
    assert set(sample.keys()) <= allowed


def test_describe_model_verbose_preserves_full_shape(tmp_path: Path) -> None:
    """verbose=true must include help / readonly / relation as before."""
    fake = _FakeClient(fields=_big_fields_get(280))
    app = _build_app(tmp_path, fake)
    dispatcher = Dispatcher(app)

    payload, size = _call(
        dispatcher,
        "odoo_describe_model",
        {"instance": "dev", "model": "res.partner", "verbose": True},
    )
    assert payload["ok"] is True
    sample = payload["fields"]["field_000"]
    assert "help" in sample
    assert "readonly" in sample
    assert "relation" in sample
    # And it should be substantially larger than the default mode.
    assert size > 100_000, f"verbose describe should be large: {size}"


# ---------------------------------------------------------------------------
# 2. search_read / read — strip Odoo-internal extras
# ---------------------------------------------------------------------------


def test_search_read_strips_unrequested_fields(tmp_path: Path) -> None:
    """Odoo may return display_name / __last_update; we must drop them."""
    records = [
        {
            "id": 1,
            "name": "Alice",
            "display_name": "Alice (contact)",
            "__last_update": "2026-04-30 12:00:00",
        },
        {
            "id": 2,
            "name": "Bob",
            "display_name": "Bob (contact)",
            "__last_update": "2026-04-30 12:00:01",
        },
    ]
    fields = {
        "id": {"type": "integer"},
        "name": {"type": "char"},
        "display_name": {"type": "char"},
        "__last_update": {"type": "datetime"},
    }
    fake = _FakeClient(fields=fields, records=records)
    app = _build_app(tmp_path, fake)
    dispatcher = Dispatcher(app)

    payload, _ = _call(
        dispatcher,
        "odoo_search_read",
        {"instance": "dev", "model": "res.partner", "fields": ["id", "name"]},
    )
    assert payload["ok"] is True
    for rec in payload["records"]:
        assert set(rec.keys()) == {"id", "name"}


def test_read_strips_unrequested_fields_but_keeps_id(tmp_path: Path) -> None:
    records = [
        {"id": 1, "name": "Alice", "display_name": "Alice", "__last_update": "x"},
    ]
    fields = {
        "id": {"type": "integer"},
        "name": {"type": "char"},
        "display_name": {"type": "char"},
        "__last_update": {"type": "datetime"},
    }
    fake = _FakeClient(fields=fields, records=records)
    app = _build_app(tmp_path, fake)
    dispatcher = Dispatcher(app)

    payload, _ = _call(
        dispatcher,
        "odoo_read",
        {"instance": "dev", "model": "res.partner", "ids": [1], "fields": ["name"]},
    )
    rec = payload["records"][0]
    assert "id" in rec  # always preserved
    assert "name" in rec
    assert "display_name" not in rec
    assert "__last_update" not in rec


# ---------------------------------------------------------------------------
# 3. read_group — drop __domain by default
# ---------------------------------------------------------------------------


def _sample_groups() -> list[dict[str, Any]]:
    return [
        {
            "stage_id": [1, "New"],
            "stage_id_count": 42,
            "__domain": [["stage_id", "=", 1]],
            "__count": 42,
        },
        {
            "stage_id": [2, "Won"],
            "stage_id_count": 17,
            "__domain": [["stage_id", "=", 2]],
            "__count": 17,
        },
    ]


def test_read_group_default_drops_domain(tmp_path: Path) -> None:
    fields = {"id": {"type": "integer"}, "stage_id": {"type": "many2one"}}
    fake = _FakeClient(fields=fields, groups=_sample_groups())
    app = _build_app(tmp_path, fake)
    dispatcher = Dispatcher(app)

    payload, _ = _call(
        dispatcher,
        "odoo_read_group",
        {
            "instance": "dev",
            "model": "crm.lead",
            "fields": ["id:count"],
            "groupby": ["stage_id"],
        },
    )
    for row in payload["groups"]:
        assert "__domain" not in row
        # __count is informative and tiny — keep it.
        assert "__count" in row


def test_read_group_include_domain_keeps_domain(tmp_path: Path) -> None:
    fields = {"id": {"type": "integer"}, "stage_id": {"type": "many2one"}}
    fake = _FakeClient(fields=fields, groups=_sample_groups())
    app = _build_app(tmp_path, fake)
    dispatcher = Dispatcher(app)

    payload, _ = _call(
        dispatcher,
        "odoo_read_group",
        {
            "instance": "dev",
            "model": "crm.lead",
            "fields": ["id:count"],
            "groupby": ["stage_id"],
            "include_domain": True,
        },
    )
    for row in payload["groups"]:
        assert "__domain" in row


# ---------------------------------------------------------------------------
# 4. odoo_help — concise mode by default
# ---------------------------------------------------------------------------


def test_help_default_stays_compact(tmp_path: Path) -> None:
    fake = _FakeClient()
    app = _build_app(tmp_path, fake)
    dispatcher = Dispatcher(app)

    payload, size = _call(dispatcher, "odoo_help", {})
    assert payload["ok"] is True
    # 1900 chars is the practical ceiling: a 14-entry tools list + summary
    # + 1 instance block ~= 1.8k. The verbose response on the same fixture
    # is ~3k. The cap exists to catch unbounded growth, not to forbid
    # adding a tool — bump it deliberately when the tool surface grows.
    assert size < 1900, f"default help payload too large: {size} chars"
    assert "tools" in payload
    # Default mode has no cookbook.
    assert "common_patterns" not in payload
    assert "gotchas" not in payload


def test_help_verbose_preserves_old_keys(tmp_path: Path) -> None:
    fake = _FakeClient()
    app = _build_app(tmp_path, fake)
    dispatcher = Dispatcher(app)

    payload, _ = _call(dispatcher, "odoo_help", {"verbose": True})
    assert payload["ok"] is True
    for key in ("version", "summary", "common_patterns", "gotchas", "instances"):
        assert key in payload


# ---------------------------------------------------------------------------
# 5. Error responses — concise hint folding
# ---------------------------------------------------------------------------


def _hint_error(message: str, hint_text: str) -> OdooMcpError:
    class _E(OdooMcpError):
        code = "test_error"

        @property
        def hint(self) -> str | None:
            return hint_text

    return _E(message)


def _run_dispatch_with_error(dispatcher: Dispatcher, err: Exception) -> dict[str, Any]:
    from odoo_mcp.dispatcher import _HANDLERS

    def _bad(_d: Dispatcher, _a: dict[str, Any]) -> dict[str, Any]:
        raise err

    original = _HANDLERS.get("odoo_help")
    _HANDLERS["odoo_help"] = _bad
    try:
        contents = asyncio.run(dispatcher.call("odoo_help", {}))
        return json.loads(contents[0].text)  # type: ignore[no-any-return]
    finally:
        if original is not None:
            _HANDLERS["odoo_help"] = original


def test_error_drops_hint_when_substring_of_error(tmp_path: Path) -> None:
    """If hint text is already inside the error message, drop hint."""
    fake = _FakeClient()
    app = _build_app(tmp_path, fake)
    dispatcher = Dispatcher(app)

    err = _hint_error(
        "Argument 'instance' must be a non-empty string. Pass instance=<name>.",
        "Pass instance=<name>.",
    )
    payload = _run_dispatch_with_error(dispatcher, err)
    assert payload["ok"] is False
    assert "hint" not in payload


def test_error_keeps_hint_when_distinct(tmp_path: Path) -> None:
    fake = _FakeClient()
    app = _build_app(tmp_path, fake)
    dispatcher = Dispatcher(app)

    err = _hint_error(
        "Permission denied on model res.users.",
        "Use odoo_list_instances to inspect allowlist mode.",
    )
    payload = _run_dispatch_with_error(dispatcher, err)
    assert payload["ok"] is False
    assert payload.get("hint") == "Use odoo_list_instances to inspect allowlist mode."
