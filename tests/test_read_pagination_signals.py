"""Truncation signalling on the read family.

``odoo_search_read`` has reported ``has_more`` / ``next_offset`` since
v0.9. Its two truncating siblings did not:

* ``odoo_read_group`` clamps to the instance hard cap (500 by default,
  *not* to a caller-visible argument) and returned only ``count``. A
  caller that sums the groups to get a total gets a silently short
  number — the failure mode of an aggregation is a wrong figure, not a
  short list.
* ``odoo_lookup`` clamps to ``min(limit, hard_cap)`` and returned only
  ``count``, so "resolve this name to an id" could not tell an exact
  match from the first of many.

These pin the signals so a future response-shape trim cannot quietly
drop them again.
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
from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD
from odoo_mcp.security.limits import RateLimiter
from odoo_mcp.security.prod_guard import ProdGuard

_HARD_CAP = 500


class _FakeClient:
    """Client that honours ``limit`` / ``offset`` the way Odoo does."""

    def __init__(
        self,
        *,
        fields: dict[str, dict[str, Any]] | None = None,
        groups: list[dict[str, Any]] | None = None,
        lookup_hits: list[dict[str, Any]] | None = None,
        over_deliver: int = 0,
    ) -> None:
        self._fields = fields if fields is not None else {"id": {"type": "integer"}}
        self._groups = groups if groups is not None else []
        self._lookup_hits = lookup_hits if lookup_hits is not None else []
        # Rows returned in excess of the requested limit, simulating a
        # third-party module that ignores it.
        self._over_deliver = over_deliver
        self.is_admin: bool | None = None
        self.admin_reason: str | None = None
        self.last_limit: int | None = None

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        return self._fields

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
        self.last_limit = limit
        window = self._groups[offset : offset + limit + self._over_deliver]
        return [dict(g) for g in window]

    def lookup(self, model: str, query: str, limit: int) -> list[dict[str, Any]]:
        self.last_limit = limit
        return [dict(r) for r in self._lookup_hits[:limit]]


def _instance_config() -> InstanceConfig:
    return InstanceConfig(
        name="dev",
        url="https://example.odoo.com",
        database="db",
        credentials_env_prefix="ODOO_MCP_DEV",
        production=False,
        timeout_seconds=30,
        max_records_default=50,
        max_records_hard_cap=_HARD_CAP,
        rate_limit_per_minute=300,
        allow_self_signed=False,
        allowed_models=frozenset({ALLOWLIST_WILDCARD}),
    )


def _build_app(tmp_path: Path, fake: _FakeClient) -> OdooMcpApp:
    cfg = _instance_config()
    creds = Credentials(instance_name=cfg.name, username="u", _api_key="k" * 10)
    app_cfg = AppConfig(
        path=tmp_path / "config.toml",
        defaults=Defaults(),
        instances={cfg.name: cfg},
        audit_log_path=tmp_path / "audit.jsonl",
    )
    rate_limiter = RateLimiter()
    rate_limiter.configure(cfg.name, cfg.rate_limit_per_minute)
    rt = InstanceRuntime(config=cfg, client=OdooClient(cfg, credentials=creds))
    rt.client = fake  # type: ignore[assignment]
    return OdooMcpApp(
        config=app_cfg,
        audit=AuditLog(app_cfg.audit_log_path),
        prod_guard=ProdGuard(),
        rate_limiter=rate_limiter,
        instances={cfg.name: rt},
    )


def _call(tmp_path: Path, fake: _FakeClient, name: str, args: dict[str, Any]) -> dict[str, Any]:
    contents = asyncio.run(Dispatcher(_build_app(tmp_path, fake)).call(name, args))
    payload: dict[str, Any] = json.loads(contents[0].text)
    return payload


def _partner_groups(n: int) -> list[dict[str, Any]]:
    return [
        {"partner_id": [i, f"Customer {i}"], "amount_total": 100.0 + i, "__count": 1}
        for i in range(1, n + 1)
    ]


_GROUP_FIELDS = {
    "id": {"type": "integer"},
    "partner_id": {"type": "many2one"},
    "amount_total": {"type": "monetary"},
}

_GROUP_ARGS = {
    "instance": "dev",
    "model": "sale.order",
    "fields": ["amount_total:sum"],
    "groupby": ["partner_id"],
}


# ---------------------------------------------------------------------------
# odoo_read_group
# ---------------------------------------------------------------------------


def test_read_group_flags_has_more_when_clamped_to_the_hard_cap(tmp_path: Path) -> None:
    """The default limit is the hard cap, not a caller argument.

    A revenue-by-customer aggregation on an instance with more customers
    than the cap must not read as a complete total.
    """
    fake = _FakeClient(fields=_GROUP_FIELDS, groups=_partner_groups(_HARD_CAP + 120))
    payload = _call(tmp_path, fake, "odoo_read_group", dict(_GROUP_ARGS))

    assert fake.last_limit == _HARD_CAP  # nothing in the args said so
    assert payload["count"] == _HARD_CAP
    assert payload["has_more"] is True
    assert payload["next_offset"] == _HARD_CAP


def test_read_group_has_more_false_on_a_partial_page(tmp_path: Path) -> None:
    """A complete aggregation must say so explicitly, not by omission."""
    fake = _FakeClient(fields=_GROUP_FIELDS, groups=_partner_groups(7))
    payload = _call(tmp_path, fake, "odoo_read_group", dict(_GROUP_ARGS))

    assert payload["count"] == 7
    assert payload["has_more"] is False
    assert "next_offset" not in payload


def test_read_group_next_offset_resumes_where_the_page_ended(tmp_path: Path) -> None:
    """``next_offset`` must be feedable straight back as ``offset``."""
    groups = _partner_groups(25)
    fake = _FakeClient(fields=_GROUP_FIELDS, groups=groups)
    first = _call(tmp_path, fake, "odoo_read_group", {**_GROUP_ARGS, "limit": 10})
    assert first["has_more"] is True
    assert first["next_offset"] == 10

    second = _call(
        tmp_path,
        _FakeClient(fields=_GROUP_FIELDS, groups=groups),
        "odoo_read_group",
        {**_GROUP_ARGS, "limit": 10, "offset": first["next_offset"]},
    )
    assert second["groups"][0]["partner_id"] == groups[10]["partner_id"]
    assert second["next_offset"] == 20


def test_read_group_next_offset_anchors_on_the_rows_received(tmp_path: Path) -> None:
    """If Odoo over-delivers, ``offset + limit`` would skip groups.

    Mirrors the same guarantee ``odoo_search_read`` makes.
    """
    fake = _FakeClient(fields=_GROUP_FIELDS, groups=_partner_groups(40), over_deliver=2)
    payload = _call(tmp_path, fake, "odoo_read_group", {**_GROUP_ARGS, "limit": 10})

    assert payload["count"] == 12
    assert payload["has_more"] is True
    assert payload["next_offset"] == 12


def test_read_group_has_more_at_the_exact_boundary(tmp_path: Path) -> None:
    """Exactly ``limit`` groups is indistinguishable from a truncated page.

    Odoo's ``read_group`` returns no total, so a full page has to be
    reported as *may* have more — the same hint contract as
    ``odoo_search_read``, never a false "complete".
    """
    fake = _FakeClient(fields=_GROUP_FIELDS, groups=_partner_groups(5))
    payload = _call(tmp_path, fake, "odoo_read_group", {**_GROUP_ARGS, "limit": 5})

    assert payload["count"] == 5
    assert payload["has_more"] is True
    assert payload["next_offset"] == 5


# ---------------------------------------------------------------------------
# odoo_lookup
# ---------------------------------------------------------------------------


_LOOKUP_FIELDS = {"id": {"type": "integer"}, "display_name": {"type": "char"}}


def _hits(n: int) -> list[dict[str, Any]]:
    return [{"id": i, "display_name": f"Acme Holding {i}"} for i in range(1, n + 1)]


def test_lookup_flags_has_more_when_the_query_is_ambiguous(tmp_path: Path) -> None:
    """Ten of three hundred matches must not read as ten matches."""
    fake = _FakeClient(fields=_LOOKUP_FIELDS, lookup_hits=_hits(300))
    payload = _call(
        tmp_path,
        fake,
        "odoo_lookup",
        {"instance": "dev", "model": "res.partner", "query": "Acme"},
    )

    assert payload["count"] == 10  # schema default
    assert payload["has_more"] is True
    # No offset argument on this tool, so no next_offset to hand back.
    assert "next_offset" not in payload


def test_lookup_has_more_false_when_all_matches_fit(tmp_path: Path) -> None:
    fake = _FakeClient(fields=_LOOKUP_FIELDS, lookup_hits=_hits(3))
    payload = _call(
        tmp_path,
        fake,
        "odoo_lookup",
        {"instance": "dev", "model": "res.partner", "query": "Acme"},
    )

    assert payload["count"] == 3
    assert payload["has_more"] is False


def test_lookup_has_more_keys_off_the_clamped_limit_not_the_request(tmp_path: Path) -> None:
    """A caller asking above the hard cap gets the cap — and is told."""
    fake = _FakeClient(fields=_LOOKUP_FIELDS, lookup_hits=_hits(_HARD_CAP + 50))
    payload = _call(
        tmp_path,
        fake,
        "odoo_lookup",
        {"instance": "dev", "model": "res.partner", "query": "Acme", "limit": 5000},
    )

    assert fake.last_limit == _HARD_CAP
    assert payload["count"] == _HARD_CAP
    assert payload["has_more"] is True
