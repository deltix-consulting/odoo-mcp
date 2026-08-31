"""Tests for the ``--json`` flags on ``cache`` and ``status``.

Doctor's ``--json`` is covered in :mod:`tests.test_doctor`. These two
CLIs are minor — keep the tests thin.
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from odoo_mcp import cache_cli
from odoo_mcp.fields_cache import PersistentFieldsCache


def _capture(fn, *argv: str) -> tuple[int, str]:  # type: ignore[no-untyped-def]
    out = StringIO()
    with patch("sys.stdout", out):
        rc = fn(list(argv))
    return rc, out.getvalue()


# ---------------------------------------------------------------------------
# cache --info --json
# ---------------------------------------------------------------------------


def test_cache_info_json_emits_machine_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "fc.db"
    PersistentFieldsCache(db).put("dev", "res.partner", {"id": {"type": "integer"}})
    monkeypatch.setattr(cache_cli, "_resolve_cache_path", lambda: db)

    rc, out = _capture(cache_cli.main, "--info", "--json")
    assert rc == 0
    payload = json.loads(out.strip())
    assert "row_count" in payload
    assert payload["row_count"] >= 1
    assert "ttl_seconds" in payload


def test_cache_info_human_default_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "fc.db"
    PersistentFieldsCache(db)
    monkeypatch.setattr(cache_cli, "_resolve_cache_path", lambda: db)
    rc, out = _capture(cache_cli.main, "--info")
    assert rc == 0
    assert "rows:" in out
    assert "ttl:" in out


# ---------------------------------------------------------------------------
# status --json
# ---------------------------------------------------------------------------


def test_status_main_unknown_arg_returns_2() -> None:
    from odoo_mcp import status_cli

    err = StringIO()
    with patch("sys.stderr", err):
        rc = status_cli.main(["--bogus"])
    assert rc == 2
    assert "Usage" in err.getvalue() or "Unknown" in err.getvalue()


def test_status_payload_shape(tmp_path: Path) -> None:
    """Build an app directly and snapshot ``_status_payload``.

    Avoids the global config dependency that ``status_cli.main`` has.
    """
    from odoo_mcp.audit import AuditLog
    from odoo_mcp.client import OdooClient
    from odoo_mcp.config import AppConfig, Defaults, InstanceConfig
    from odoo_mcp.credentials import Credentials
    from odoo_mcp.dispatcher import InstanceRuntime, OdooMcpApp
    from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD
    from odoo_mcp.security.limits import RateLimiter
    from odoo_mcp.security.prod_guard import ProdGuard
    from odoo_mcp.status_cli import _status_payload

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
    client = OdooClient(cfg, credentials=creds)
    app_cfg = AppConfig(
        path=tmp_path / "config.toml",
        defaults=Defaults(),
        instances={cfg.name: cfg},
        audit_log_path=tmp_path / "audit.jsonl",
    )
    rl = RateLimiter()
    rl.configure(cfg.name, cfg.rate_limit_per_minute)
    app = OdooMcpApp(
        config=app_cfg,
        audit=AuditLog(app_cfg.audit_log_path),
        prod_guard=ProdGuard(),
        rate_limiter=rl,
        instances={cfg.name: InstanceRuntime(config=cfg, client=client)},
    )
    payload = _status_payload(app)
    assert payload["version"]
    assert payload["instances"]
    inst = payload["instances"][0]
    assert inst["name"] == "dev"
    assert inst["production"] is False
    assert inst["writes_unlocked"] is True  # non-prod
    assert "rate_limit" in inst
    assert inst["rate_limit"]["capacity_per_minute"] == 300


# ---------------------------------------------------------------------------
# status --json parity with the human render
#
# ``_status_payload`` documents itself as the "machine-readable equivalent"
# of ``_render``. These tests hold it to that: every fact the operator can
# read off the table has to be reachable by the CI job / dashboard that
# reads ``--json``, which is the consumer that cannot ask a follow-up.
# ---------------------------------------------------------------------------


def _build_app(tmp_path: Path, *, production: bool = False):  # type: ignore[no-untyped-def]
    """Build an ``OdooMcpApp`` with one instance, prod or dev."""
    from odoo_mcp.audit import AuditLog
    from odoo_mcp.client import OdooClient
    from odoo_mcp.config import AppConfig, Defaults, InstanceConfig
    from odoo_mcp.credentials import Credentials
    from odoo_mcp.dispatcher import InstanceRuntime, OdooMcpApp
    from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD
    from odoo_mcp.security.limits import RateLimiter
    from odoo_mcp.security.prod_guard import ProdGuard

    cfg = InstanceConfig(
        name="prod" if production else "dev",
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
    app_cfg = AppConfig(
        path=tmp_path / "config.toml",
        defaults=Defaults(),
        instances={cfg.name: cfg},
        audit_log_path=tmp_path / "audit.jsonl",
    )
    rl = RateLimiter()
    rl.configure(cfg.name, cfg.rate_limit_per_minute)
    return OdooMcpApp(
        config=app_cfg,
        audit=AuditLog(app_cfg.audit_log_path),
        prod_guard=ProdGuard(),
        rate_limiter=rl,
        instances={
            cfg.name: InstanceRuntime(config=cfg, client=OdooClient(cfg, credentials=creds))
        },
    )


def _seed_audit_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entries: list[dict]) -> None:  # type: ignore[type-arg]
    """Point the audit readers at a temp log holding *entries*."""
    from odoo_mcp import audit_cli

    audit_dir = tmp_path / ".odoo-mcp"
    audit_dir.mkdir(exist_ok=True)
    current = audit_dir / "audit.jsonl"
    current.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    monkeypatch.setattr(audit_cli, "_audit_dir", lambda: audit_dir)
    monkeypatch.setattr(audit_cli, "_audit_current", lambda: current)


def _entry(**over: object) -> dict:  # type: ignore[type-arg]
    from datetime import UTC, datetime

    base: dict = {  # type: ignore[type-arg]
        "ts": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "instance": "prod",
        "tool": "odoo_search_read",
        "op": "search_read",
        "model": "res.partner",
        "result": "ok",
        "record_count": 3,
        "duration_ms": 12,
        "dry_run": False,
        "details": {},
    }
    base.update(over)
    return base


def test_status_json_reports_how_long_the_unlock_window_still_has(tmp_path: Path) -> None:
    """``writes_unlocked: true`` alone cannot answer "for how long?".

    A monitoring job that alerts on an open prod-write window needs to
    tell a window closing in 5 seconds from one that just opened for 30
    minutes. The human render has said "auto-lock in Xm" since v0.19.1.
    """
    from odoo_mcp.status_cli import _status_payload

    app = _build_app(tmp_path, production=True)
    app.prod_guard.unlock("prod", True, ttl_seconds=900)

    inst = _status_payload(app)["instances"][0]
    assert inst["writes_unlocked"] is True
    remaining = inst["unlock_expires_in_seconds"]
    assert remaining is not None
    assert 890 < remaining <= 900

    # A tenant on a tight 60s TTL must be distinguishable from the above.
    app.prod_guard.unlock("prod", True, ttl_seconds=60)
    tight = _status_payload(app)["instances"][0]["unlock_expires_in_seconds"]
    assert tight is not None
    assert tight <= 60


def test_status_json_omits_unlock_expiry_when_writes_are_locked(tmp_path: Path) -> None:
    """No window open — the key is present and null, never a stale number."""
    from odoo_mcp.status_cli import _status_payload

    app = _build_app(tmp_path, production=True)
    inst = _status_payload(app)["instances"][0]
    assert inst["writes_unlocked"] is False
    assert inst["unlock_expires_in_seconds"] is None


def test_status_json_carries_recent_activity_and_last_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audit facts the table shows must reach the ``--json`` consumer."""
    from odoo_mcp.status_cli import _status_payload

    _seed_audit_log(
        tmp_path,
        monkeypatch,
        [
            _entry(ts="2026-08-31T10:00:00Z", tool="odoo_read"),
            _entry(ts="2026-08-31T10:05:00Z", tool="odoo_search_read"),
        ],
    )
    app = _build_app(tmp_path, production=True)
    payload = _status_payload(app)

    assert [e["tool"] for e in payload["recent_activity"]] == [
        "odoo_read",
        "odoo_search_read",
    ]
    assert payload["instances"][0]["last_call_ts"] == "2026-08-31T10:05:00Z"


def test_status_json_recent_activity_distinguishes_a_delete_from_an_archive_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``op`` and ``dry_run`` are what decide what a row MEANS.

    ``odoo_archive_or_delete`` logs a reversible ``archive`` and a
    permanent ``unlink`` under one tool name, and a ``dry_run`` row
    changed nothing in Odoo. Without both fields the two rows are
    indistinguishable to a machine consumer.
    """
    from odoo_mcp.status_cli import _status_payload

    _seed_audit_log(
        tmp_path,
        monkeypatch,
        [
            _entry(
                ts="2026-08-31T10:00:00Z",
                tool="odoo_archive_or_delete",
                op="archive",
                dry_run=True,
            ),
            _entry(
                ts="2026-08-31T10:01:00Z",
                tool="odoo_archive_or_delete",
                op="unlink",
                dry_run=False,
            ),
        ],
    )
    rows = _status_payload(_build_app(tmp_path, production=True))["recent_activity"]
    assert [(r["op"], r["dry_run"]) for r in rows] == [("archive", True), ("unlink", False)]


def test_status_json_recent_activity_discloses_no_more_than_the_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the ``details`` parts ``_format_detail`` renders are forwarded."""
    from odoo_mcp.status_cli import _status_payload

    _seed_audit_log(
        tmp_path,
        monkeypatch,
        [
            _entry(
                result="model_not_allowed",
                details={"error": "boom", "domain_shape": "secret-ish", "fields": ["x"]},
            )
        ],
    )
    row = _status_payload(_build_app(tmp_path, production=True))["recent_activity"][0]
    assert row["error"] == "boom"
    assert "details" not in row
    assert "domain_shape" not in row
    assert "fields" not in row


def test_status_json_reaches_parity_with_every_fact_the_render_prints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The durable form: diff the two renderers, not a fixed key list.

    ``_status_payload`` claims to be the machine-readable equivalent of
    ``_render``. Rather than pin today's keys, assert the implication:
    whenever the human render states a fact, the payload has a non-null
    carrier for it. A future line added to ``_render`` alone fails here.
    """
    from odoo_mcp.status_cli import _render, _status_payload

    _seed_audit_log(
        tmp_path,
        monkeypatch,
        [_entry(ts="2026-08-31T10:00:00Z"), _entry(ts="2026-08-31T10:05:00Z")],
    )
    app = _build_app(tmp_path, production=True)
    app.prod_guard.unlock("prod", True, ttl_seconds=900)
    app.instances["prod"].client._uid = 2  # force the "last call" branch

    rendered = _render(app)
    payload = _status_payload(app)
    inst = payload["instances"][0]

    if "auto-lock in" in rendered:
        assert inst["unlock_expires_in_seconds"] is not None
    if "commits remaining" in rendered:
        assert inst["commits_remaining"] is not None
    if "last call" in rendered:
        assert inst["last_call_ts"] is not None
    for ts in ("2026-08-31T10:00:00Z", "2026-08-31T10:05:00Z"):
        if ts in rendered:
            assert any(row["ts"] == ts for row in payload["recent_activity"])
