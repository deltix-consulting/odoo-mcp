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

    # ``_audit_current`` is now the single source for both the current log
    # and the directory its rotations live in, so only one patch is needed.
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
# The reviewer must read the log the server writes.
#
# Every other consumer of ``[defaults] audit_log`` honours it — the server
# opens ``cfg.audit_log_path``, doctor probes it for writability, and both
# ``config show`` and ``status`` print it. ``audit_cli`` alone resolved
# ``DEFAULT_AUDIT_LOG`` directly, so on an install that sets ``audit_log``
# the review CLI read an unrelated (usually absent) file and reported "no
# entries" — indistinguishable from a quiet system. These tests drive the
# real ``_audit_current`` against a real config file rather than patching it
# out, which is what let the gap survive.
# ---------------------------------------------------------------------------


def _write_cfg_with_audit_log(tmp_path, audit_log):  # type: ignore[no-untyped-def]
    import os

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[defaults]\n"
        f'audit_log = "{audit_log}"\n'
        "\n"
        "[instances.dev]\n"
        'url = "https://dev.example.odoo.com"\n'
        'database = "dev_db"\n'
        'credentials_env_prefix = "ODOO_MCP_DEV"\n'
        "production = false\n"
    )
    if os.name == "posix":
        cfg.chmod(0o600)
    return cfg


def test_audit_current_follows_configured_audit_log(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    """``[defaults] audit_log`` decides which file ``odoo-mcp audit`` reads."""
    from odoo_mcp import config as config_mod

    configured = tmp_path / "logs" / "audit.jsonl"
    configured.parent.mkdir()
    cfg = _write_cfg_with_audit_log(tmp_path, configured)
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", cfg)

    assert audit_cli._audit_current() == configured


def test_audit_reads_entries_from_the_configured_log(tmp_path, monkeypatch, capsys):  # type: ignore[no-untyped-def]
    """An entry written to the configured log shows up in ``audit --tail``.

    The regression: it used to land in the configured file and be invisible
    to the CLI, which was reading ``~/.odoo-mcp/audit.jsonl``.
    """
    import json as _json

    from odoo_mcp import config as config_mod

    configured = tmp_path / "logs" / "audit.jsonl"
    configured.parent.mkdir()
    entry = _entry(datetime.now(tz=UTC), tool="odoo_write", instance="prod")
    configured.write_text(_json.dumps(entry) + "\n")

    cfg = _write_cfg_with_audit_log(tmp_path, configured)
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", cfg)

    assert audit_cli.main(["--tail", "5"]) == 0
    out = capsys.readouterr().out
    assert "odoo_write" in out
    assert "no audit entries" not in out


def test_audit_falls_back_to_default_when_config_is_unloadable(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    """A broken/absent config must not break the forensics command."""
    from pathlib import Path

    from odoo_mcp import config as config_mod
    from odoo_mcp.config import DEFAULT_AUDIT_LOG

    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", tmp_path / "does-not-exist.toml")

    assert audit_cli._audit_current() == Path(DEFAULT_AUDIT_LOG).expanduser()


def test_rotations_are_resolved_beside_the_configured_log(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    """Rotated files are found next to the configured log, not next to the default.

    ``AuditLog`` writes rotations with ``Path.with_name``, so they always sit
    in the current log's directory. Deriving the scan directory independently
    of the current log is what allowed the two to point at different places.
    """
    from pathlib import Path

    from odoo_mcp import config as config_mod

    logs = tmp_path / "logs"
    logs.mkdir()
    current = logs / "audit.jsonl"
    current.write_text("")
    yesterday = (datetime.now(tz=UTC) - timedelta(days=1)).date()
    rotated = logs / f"audit-{yesterday.isoformat()}.jsonl"
    rotated.write_text("")

    cfg = _write_cfg_with_audit_log(tmp_path, current)
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", cfg)

    # Compare full paths, not basenames: the developer's own
    # ``~/.odoo-mcp`` holds an ``audit.jsonl`` plus dated rotations, so a
    # name-only assertion passes even when the scan read the wrong directory.
    found = {Path(f) for f in audit_cli._audit_files(since_minutes=24 * 60)}
    assert found == {current, rotated}


def test_status_reads_the_same_log_it_prints(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    """``status`` must not print one path and tabulate rows from another.

    Asserts the implication rather than the call: whatever path the report
    names is the path the "Recent activity" rows were loaded from.
    """
    from pathlib import Path

    from odoo_mcp import status_cli

    seen: list[Path | None] = []

    def _spy(*, since_minutes=None, path=None):  # type: ignore[no-untyped-def]
        seen.append(path)
        return []

    monkeypatch.setattr(status_cli, "_load_all_entries", _spy)

    class _Cfg:
        path = tmp_path / "config.toml"
        audit_log_path = tmp_path / "logs" / "audit.jsonl"

    class _App:
        config = _Cfg()
        instances: dict[str, Any] = {}

    rendered = status_cli._render(_App())  # type: ignore[arg-type]

    assert seen == [_Cfg.audit_log_path]
    assert str(_Cfg.audit_log_path) in rendered
