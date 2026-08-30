"""Tests for odoo_mcp.audit_cli filtering and rendering."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from odoo_mcp import audit_cli


def _entry(
    ts: datetime,
    *,
    result: str = "ok",
    instance: str = "prod",
    tool: str = "odoo_search_read",
    model: str | None = "res.partner",
    record_count: int | None = 10,
    duration_ms: int = 42,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "instance": instance,
        "tool": tool,
        "op": "search_read",
        "model": model,
        "result": result,
        "record_count": record_count,
        "duration_ms": duration_ms,
        "dry_run": False,
        "details": details or {},
    }


def test_filter_errors_only_within_24h() -> None:
    now = datetime(2026, 4, 16, 18, 0, 0, tzinfo=UTC)
    entries = [
        _entry(now - timedelta(hours=1), result="ok"),
        _entry(now - timedelta(hours=2), result="prod_guard"),
        _entry(now - timedelta(hours=48), result="prod_guard"),  # too old
    ]
    out = audit_cli._filter(entries, errors_only=True, instance=None, since_minutes=None, now=now)
    assert len(out) == 1
    assert out[0]["result"] == "prod_guard"


def test_filter_by_instance() -> None:
    now = datetime(2026, 4, 16, 18, 0, 0, tzinfo=UTC)
    entries = [
        _entry(now, instance="prod"),
        _entry(now, instance="dev"),
    ]
    out = audit_cli._filter(entries, errors_only=False, instance="dev", since_minutes=None, now=now)
    assert len(out) == 1
    assert out[0]["instance"] == "dev"


def test_filter_since_minutes() -> None:
    now = datetime(2026, 4, 16, 18, 0, 0, tzinfo=UTC)
    entries = [
        _entry(now - timedelta(minutes=10)),
        _entry(now - timedelta(minutes=120)),
    ]
    out = audit_cli._filter(entries, errors_only=False, instance=None, since_minutes=60, now=now)
    assert len(out) == 1


def test_filter_combinations() -> None:
    now = datetime(2026, 4, 16, 18, 0, 0, tzinfo=UTC)
    entries = [
        _entry(now - timedelta(minutes=5), instance="prod", result="prod_guard"),
        _entry(now - timedelta(minutes=5), instance="dev", result="prod_guard"),
        _entry(now - timedelta(minutes=5), instance="prod", result="ok"),
    ]
    out = audit_cli._filter(entries, errors_only=True, instance="prod", since_minutes=60, now=now)
    assert len(out) == 1
    assert out[0]["instance"] == "prod"
    assert out[0]["result"] == "prod_guard"


def test_format_detail_with_error() -> None:
    now = datetime(2026, 4, 16, 18, 0, 0, tzinfo=UTC)
    entry = _entry(
        now,
        record_count=None,
        duration_ms=3,
        details={"error": "something went wrong"},
    )
    out = audit_cli._format_detail(entry)
    assert "3ms" in out
    assert "something went wrong" in out


def test_render_table_has_header() -> None:
    now = datetime(2026, 4, 16, 18, 0, 0, tzinfo=UTC)
    entries = [_entry(now)]
    table = audit_cli._render_table(entries)
    assert "TIME" in table
    assert "RESULT" in table
    assert "res.partner" in table


# ---------------------------------------------------------------------------
# Per-tool stats (--stats)
# ---------------------------------------------------------------------------


def test_percentile_empty_returns_zero() -> None:
    assert audit_cli._percentile([], 50) == 0
    assert audit_cli._percentile([], 95) == 0


def test_percentile_single_value() -> None:
    assert audit_cli._percentile([7], 50) == 7
    assert audit_cli._percentile([7], 95) == 7


def test_percentile_known_distribution() -> None:
    sample = list(range(1, 101))  # 1..100, sorted
    assert audit_cli._percentile(sample, 50) == 50 or audit_cli._percentile(sample, 50) == 51
    assert audit_cli._percentile(sample, 95) >= 95


def test_render_stats_groups_by_tool() -> None:
    now = datetime(2026, 4, 16, 18, 0, 0, tzinfo=UTC)
    entries = [
        _entry(now, tool="odoo_search_read", duration_ms=10),
        _entry(now, tool="odoo_search_read", duration_ms=30),
        _entry(now, tool="odoo_search_read", duration_ms=200),
        _entry(now, tool="odoo_read", duration_ms=5),
    ]
    out = audit_cli._render_stats(entries)
    # Header
    assert "TOOL" in out
    assert "P50ms" in out
    assert "P95ms" in out
    assert "MAXms" in out
    # Both tools appear
    assert "odoo_search_read" in out
    assert "odoo_read" in out
    # Max for search_read should be 200
    assert "200" in out


def test_render_stats_counts_errors_separately() -> None:
    now = datetime(2026, 4, 16, 18, 0, 0, tzinfo=UTC)
    entries = [
        _entry(now, tool="odoo_create", result="ok", duration_ms=20),
        _entry(now, tool="odoo_create", result="prod_guard_error", duration_ms=2),
        _entry(now, tool="odoo_create", result="prod_guard_error", duration_ms=1),
    ]
    out = audit_cli._render_stats(entries)
    lines = [line for line in out.splitlines() if "odoo_create" in line]
    assert len(lines) == 1
    # Format: TOOL CALLS OK ERR P50 P95 MAX
    parts = lines[0].split()
    # parts[0] is tool, parts[1] is calls, parts[2] is ok, parts[3] is err
    assert parts[1] == "3"
    assert parts[2] == "1"
    assert parts[3] == "2"


def test_render_stats_empty_input() -> None:
    out = audit_cli._render_stats([])
    assert "no audit entries" in out


def test_stats_payload_shape() -> None:
    now = datetime(2026, 4, 16, 18, 0, 0, tzinfo=UTC)
    entries = [
        _entry(now, tool="odoo_read", duration_ms=10),
        _entry(now, tool="odoo_read", result="error", duration_ms=200),
    ]
    payload = audit_cli._stats_payload(entries)
    assert isinstance(payload, list)
    assert len(payload) == 1
    row = payload[0]
    assert row["tool"] == "odoo_read"
    assert row["calls"] == 2
    assert row["ok"] == 1
    assert row["err"] == 1
    assert "p50_ms" in row
    assert "p95_ms" in row
    assert row["max_ms"] == 200


def test_stats_payload_sorted_by_calls_desc() -> None:
    now = datetime(2026, 4, 16, 18, 0, 0, tzinfo=UTC)
    entries = [
        _entry(now, tool="odoo_read", duration_ms=10),
        _entry(now, tool="odoo_search_read", duration_ms=10),
        _entry(now, tool="odoo_search_read", duration_ms=10),
        _entry(now, tool="odoo_search_read", duration_ms=10),
    ]
    payload = audit_cli._stats_payload(entries)
    assert payload[0]["tool"] == "odoo_search_read"
    assert payload[1]["tool"] == "odoo_read"


# ---------------------------------------------------------------------------
# Audit log file selection (perf optimization)
# ---------------------------------------------------------------------------


def test_audit_files_excludes_old_rotations_when_since_set(  # type: ignore[no-untyped-def]
    tmp_path, monkeypatch
):
    """With since_minutes set, rotated files older than the cutoff are skipped.

    The fix: status / audit --since N / audit --errors should not load
    30 days of rotation history just to throw it away. Verify by
    sprinkling dated rotation files and checking which ones are picked.
    """
    from pathlib import Path

    audit_dir = tmp_path / ".odoo-mcp"
    audit_dir.mkdir()
    current = audit_dir / "audit.jsonl"
    current.write_text("")
    today = datetime.now(tz=UTC).date()
    for delta in (1, 2, 3, 10, 30):
        d = today - timedelta(days=delta)
        (audit_dir / f"audit-{d.isoformat()}.jsonl").write_text("")

    monkeypatch.setattr(audit_cli, "_audit_dir", lambda: audit_dir)
    monkeypatch.setattr(audit_cli, "_audit_current", lambda: current)

    # 24h window: only the 1-day-old file qualifies (plus current).
    files_24h = audit_cli._audit_files(since_minutes=24 * 60)
    names = {Path(f).name for f in files_24h}
    assert "audit.jsonl" in names
    assert f"audit-{(today - timedelta(days=1)).isoformat()}.jsonl" in names
    assert f"audit-{(today - timedelta(days=10)).isoformat()}.jsonl" not in names
    assert f"audit-{(today - timedelta(days=30)).isoformat()}.jsonl" not in names

    # 5-day window: the 1-, 2-, 3-day rotations qualify, not 10 or 30.
    files_5d = audit_cli._audit_files(since_minutes=5 * 24 * 60)
    names = {Path(f).name for f in files_5d}
    assert f"audit-{(today - timedelta(days=3)).isoformat()}.jsonl" in names
    assert f"audit-{(today - timedelta(days=10)).isoformat()}.jsonl" not in names

    # No window: every rotation included.
    files_all = audit_cli._audit_files(since_minutes=None)
    names = {Path(f).name for f in files_all}
    assert f"audit-{(today - timedelta(days=30)).isoformat()}.jsonl" in names


# ---------------------------------------------------------------------------
# Renderer vs. record shape
#
# The audit table is the operator's review surface (SECURITY.md checklist:
# "Audit log is being reviewed at a real cadence"). Every field the record
# carries has to reach it, or the reviewer draws a conclusion the row does
# not support.
# ---------------------------------------------------------------------------

# One probe per AuditEvent field: the value to log, and the token that must
# show up in the rendered row. ``ts`` is added by AuditLog.log() around the
# event, so it is probed separately below.
_FIELD_PROBES: dict[str, tuple[Any, str]] = {
    "instance": ("acme-prod", "acme-prod"),
    "tool": ("odoo_archive_or_delete", "odoo_archive_or_delete"),
    "op": ("unlink", "unlink"),
    "model": ("sale.order", "sale.order"),
    "result": ("ok", "ok"),
    "record_count": (3, "3 records"),
    "duration_ms": (42, "42ms"),
    "dry_run": (True, "dry-run"),
    "details": ({"error": "boom"}, "boom"),
}


def _probe_entry() -> dict[str, Any]:
    entry: dict[str, Any] = {"ts": "2026-08-30T09:00:00Z"}
    entry.update({name: value for name, (value, _) in _FIELD_PROBES.items()})
    return entry


def test_render_table_surfaces_every_audit_event_field() -> None:
    """Every :class:`AuditEvent` field must reach the rendered row.

    Walks the dataclass rather than a hand-written list, so adding a field
    to the audit record without giving it an output line fails here instead
    of silently disappearing from the operator's view.
    """
    import dataclasses

    from odoo_mcp.audit import AuditEvent

    declared = {f.name for f in dataclasses.fields(AuditEvent)}
    assert declared == set(_FIELD_PROBES), (
        "AuditEvent fields changed; add a probe (value + expected token) for "
        f"{declared ^ set(_FIELD_PROBES)} and render it in _render_table."
    )

    table = audit_cli._render_table([_probe_entry()])
    assert "2026-08-30T09:00:00Z" in table
    for name, (_, token) in _FIELD_PROBES.items():
        assert token in table, f"audit table drops the {name!r} field ({token!r} missing)"


def test_render_table_separates_archive_preview_from_permanent_delete() -> None:
    """The two odoo_archive_or_delete outcomes must not render identically.

    ``mode='archive'`` logs op=archive (reversible) and ``mode='delete'``
    logs op=unlink (permanent); a dry run changes nothing in Odoo at all.
    Both distinctions live only in ``op`` / ``dry_run``.
    """

    def row(op: str, dry_run: bool) -> dict[str, Any]:
        return {
            "ts": "2026-08-30T09:00:00Z",
            "instance": "prod",
            "tool": "odoo_archive_or_delete",
            "op": op,
            "model": "sale.order",
            "result": "ok",
            "record_count": 3,
            "duration_ms": 42,
            "dry_run": dry_run,
            "details": {},
        }

    committed_delete, archive_preview = audit_cli._render_table(
        [row("unlink", False), row("archive", True)]
    ).splitlines()[1:]
    assert committed_delete != archive_preview
    assert "unlink" in committed_delete and "dry-run" not in committed_delete
    assert "archive" in archive_preview and "dry-run" in archive_preview


def test_status_recent_activity_shares_the_audit_row_shape(monkeypatch: Any, tmp_path: Any) -> None:
    """``odoo-mcp status`` renders the same rows and must not drop the same fields."""
    from odoo_mcp import status_cli
    from odoo_mcp.audit import AuditLog
    from odoo_mcp.client import OdooClient
    from odoo_mcp.config import AppConfig, Defaults, InstanceConfig
    from odoo_mcp.credentials import Credentials
    from odoo_mcp.dispatcher import InstanceRuntime, OdooMcpApp
    from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD
    from odoo_mcp.security.limits import RateLimiter
    from odoo_mcp.security.prod_guard import ProdGuard

    cfg = InstanceConfig(
        name="prod",
        url="https://example.odoo.com",
        database="db",
        credentials_env_prefix="ODOO_MCP_PROD",
        production=True,
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
    app = OdooMcpApp(
        config=app_cfg,
        audit=AuditLog(app_cfg.audit_log_path),
        prod_guard=ProdGuard(),
        rate_limiter=rl,
        instances={
            cfg.name: InstanceRuntime(config=cfg, client=OdooClient(cfg, credentials=creds))
        },
    )
    entry = _probe_entry() | {"instance": "prod"}
    monkeypatch.setattr(status_cli, "_load_all_entries", lambda **_: [entry])

    out = status_cli._render(app)
    for name, (_, token) in _FIELD_PROBES.items():
        if name == "instance":
            token = "prod"
        assert token in out, f"status recent-activity drops the {name!r} field ({token!r} missing)"
