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
