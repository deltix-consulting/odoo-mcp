"""An audit log that cannot be READ must never be reported as an empty log.

``AuditLog`` already fails closed when the log is unwritable
(``test_audit.py::test_open_fails_closed_when_unwritable``). The reader
did the opposite: every OS error was swallowed into ``[]`` and rendered
as "(no audit entries match the filters)" with exit code 0 — a forensic
tool answering "nothing happened" when the truth was "I could not look".

These tests pin the reader to the writer's posture: report what could not
be read, and do not exit 0 when nothing could be.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from odoo_mcp import audit_cli

_POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="Relies on POSIX chmod semantics denying the current user",
)


def _row(ts: str, *, tool: str = "odoo_search_read", result: str = "ok") -> str:
    return json.dumps(
        {
            "ts": ts,
            "instance": "prod",
            "tool": tool,
            "model": "res.partner",
            "result": result,
            "record_count": 3,
            "duration_ms": 12,
        }
    )


@pytest.fixture()
def audit_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI at an isolated audit directory holding two good rows."""
    audit_dir = tmp_path / ".odoo-mcp"
    audit_dir.mkdir()
    current = audit_dir / "audit.jsonl"
    current.write_text(
        _row("2026-08-18T09:00:00Z") + "\n" + _row("2026-08-18T09:01:00Z", result="error") + "\n"
    )
    monkeypatch.setattr(audit_cli, "_audit_dir", lambda: audit_dir)
    monkeypatch.setattr(audit_cli, "_audit_current", lambda: current)
    return current


# ---------------------------------------------------------------------------
# The fix: unreadable is not empty
# ---------------------------------------------------------------------------


@_POSIX_ONLY
def test_unreadable_log_is_not_reported_as_no_activity(
    audit_log: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    audit_log.chmod(0o000)
    try:
        rc = audit_cli.main([])
    finally:
        audit_log.chmod(0o600)
    captured = capsys.readouterr()

    # The load-bearing claim: the operator is told the log was unreadable,
    # not that the filters matched nothing.
    assert captured.out.strip() == audit_cli._UNREADABLE_MESSAGE
    assert captured.out.strip() != audit_cli._NO_MATCH_MESSAGE
    assert "unreadable" in captured.err
    # A command that could not do its job must not exit 0 — a monitoring
    # script running `audit --errors` would otherwise read a broken log
    # as a clean bill of health.
    assert rc == 1


@_POSIX_ONLY
def test_unreadable_audit_directory_does_not_crash(
    audit_log: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unreadable *parent* made even ``Path.exists()`` raise.

    ``_audit_files`` guarded only ``iterdir()``, so the stat of
    ``audit.jsonl`` escaped as an uncaught ``PermissionError`` traceback.
    """
    audit_dir = audit_log.parent
    audit_dir.chmod(0o000)
    try:
        rc = audit_cli.main([])
    finally:
        audit_dir.chmod(0o700)
    captured = capsys.readouterr()

    assert rc == 1
    assert captured.out.strip() == audit_cli._UNREADABLE_MESSAGE
    assert "unreadable" in captured.err


@_POSIX_ONLY
def test_json_stdout_stays_a_bare_list_and_warns_on_stderr(
    audit_log: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--json`` must keep its stdout contract; the warning goes to stderr.

    Machine consumers pipe stdout into jq, so the diagnostic cannot go
    there. The exit code is what makes the failure detectable.
    """
    audit_log.chmod(0o000)
    try:
        rc = audit_cli.main(["--json"])
    finally:
        audit_log.chmod(0o600)
    captured = capsys.readouterr()

    assert json.loads(captured.out) == []
    assert "unreadable" in captured.err
    assert rc == 1


@_POSIX_ONLY
def test_stats_view_also_reports_an_unreadable_log(
    audit_log: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--stats`` renders through a different branch and was equally silent."""
    audit_log.chmod(0o000)
    try:
        rc = audit_cli.main(["--stats"])
    finally:
        audit_log.chmod(0o600)
    captured = capsys.readouterr()

    assert captured.out.strip() == audit_cli._UNREADABLE_MESSAGE
    assert rc == 1


def test_unparseable_rows_are_counted_and_reported(
    audit_log: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A partially corrupt log still renders, but says what it dropped.

    A truncated write or a hand-edited line is the most interesting row in
    a forensic log; it must not vanish into a clean-looking table.
    """
    with audit_log.open("a") as f:
        f.write('{"ts": "2026-08-18T09:02:00Z", "tool": "odoo_unl\n')  # truncated
        f.write("not json at all\n")

    rc = audit_cli.main([])
    captured = capsys.readouterr()

    # The good rows still render, and the run succeeds...
    assert "odoo_search_read" in captured.out
    assert rc == 0
    # ...but the two unusable lines are accounted for.
    assert "2 line(s) skipped" in captured.err


def test_open_markers_are_not_counted_as_unparseable(
    audit_log: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Control: ``audit_log_open`` markers are expected bookkeeping.

    Counting them would make every healthy log warn, which would train
    the operator to ignore the warning.
    """
    with audit_log.open("a") as f:
        f.write(json.dumps({"event": "audit_log_open", "ts": "2026-08-18T08:00:00Z"}) + "\n")

    rc = audit_cli.main([])
    captured = capsys.readouterr()

    assert captured.err == ""
    assert rc == 0


# ---------------------------------------------------------------------------
# Controls: the quiet paths must stay quiet
# ---------------------------------------------------------------------------


def test_absent_log_is_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A fresh install has no audit.jsonl — that IS "no entries", not a fault."""
    audit_dir = tmp_path / ".odoo-mcp"
    audit_dir.mkdir()
    monkeypatch.setattr(audit_cli, "_audit_dir", lambda: audit_dir)
    monkeypatch.setattr(audit_cli, "_audit_current", lambda: audit_dir / "audit.jsonl")

    rc = audit_cli.main([])
    captured = capsys.readouterr()

    assert captured.out.strip() == audit_cli._NO_MATCH_MESSAGE
    assert captured.err == ""
    assert rc == 0


def test_healthy_log_reports_nothing_and_exits_zero(
    audit_log: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Control: a clean log must produce no warnings at all."""
    rc = audit_cli.main([])
    captured = capsys.readouterr()

    assert captured.err == ""
    assert rc == 0
    assert "odoo_search_read" in captured.out


def test_load_all_entries_wrapper_still_returns_entries_only(audit_log: Path) -> None:
    """Control: the entries-only helper keeps its shape for existing callers."""
    entries = audit_cli._load_all_entries()
    assert isinstance(entries, list)
    assert [e["tool"] for e in entries] == ["odoo_search_read", "odoo_search_read"]


def test_load_entries_returns_entries_and_issues(audit_log: Path) -> None:
    entries, issues = audit_cli._load_entries()
    assert len(entries) == 2
    assert issues == []


# ---------------------------------------------------------------------------
# `odoo-mcp status` shares the loader, and made the same claim
# ---------------------------------------------------------------------------


class _StubConfig:
    path = Path("/tmp/config.toml")
    audit_log_path = Path("/tmp/audit.jsonl")


class _StubApp:
    """Minimal stand-in for OdooMcpApp — `_render` skips the per-instance
    loop (and therefore the rate limiter and prod guard) when there are
    no instances, which is all the recent-activity block needs."""

    config = _StubConfig()
    instances: dict[str, object] = {}


@_POSIX_ONLY
def test_status_says_the_audit_log_is_unreadable(audit_log: Path) -> None:
    """`status` printed "(no audit entries yet)" — reassuring and wrong."""
    from odoo_mcp import status_cli

    audit_log.chmod(0o000)
    try:
        out = status_cli._render(_StubApp())  # type: ignore[arg-type]
    finally:
        audit_log.chmod(0o600)

    assert "(no audit entries yet)" not in out
    assert "could not be read" in out
    assert "unreadable" in out


def test_status_stays_quiet_on_a_healthy_log(audit_log: Path) -> None:
    """Control: a readable log must not make `status` cry wolf."""
    from odoo_mcp import status_cli

    out = status_cli._render(_StubApp())  # type: ignore[arg-type]

    assert "warning" not in out
    assert "odoo_search_read" in out
