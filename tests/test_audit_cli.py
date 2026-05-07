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
