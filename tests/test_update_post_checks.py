"""``odoo-mcp update`` must not report success on checks it never looked at.

The update runs eight sub-commands. Six of them had their exit status
checked; two of the post-pull steps — the ``uv tool install`` shim refresh
and ``doctor`` — had their results discarded, so an update that left the
install failing its own health check still printed "Update complete." and
exited 0.

``tests/test_update_cli.py`` covers the migration helpers and ``--check``
but never drives ``main()``, so the whole apply path was untested.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from odoo_mcp import doctor, update_cli


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> Any:
    return subprocess.CompletedProcess(
        args=["fake"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class _Harness:
    """Drives ``update_cli.main([])`` all the way to the post-update checks."""

    def __init__(self) -> None:
        self.shim_rc = 0
        self.shim_stderr = ""
        self.tests_rc = 0
        self.doctor_rc = 0
        self.ran: list[list[str]] = []

    def git(self, project_dir: Path, *args: str, check: bool = True) -> Any:
        if args[:1] == ("fetch",):
            return _completed()
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return _completed(stdout="main\n")
        if args == ("rev-parse", "HEAD"):
            return _completed(stdout="aaaaaaa\n")
        if args == ("rev-parse", "origin/main"):
            return _completed(stdout="bbbbbbb\n")
        if args[:1] == ("log",):
            return _completed(stdout="bbbbbbb newer commit\n")
        if args[:2] == ("merge", "--ff-only"):
            return _completed()
        raise AssertionError(f"unexpected git {args}")

    def run(self, cmd: list[str], cwd: Path) -> Any:
        self.ran.append(cmd)
        if cmd[:2] == ["uv", "sync"]:
            return _completed()
        if cmd[:3] == ["uv", "tool", "install"]:
            return _completed(returncode=self.shim_rc, stderr=self.shim_stderr)
        if cmd[:3] == ["uv", "run", "pytest"]:
            return _completed(returncode=self.tests_rc)
        raise AssertionError(f"unexpected command {cmd}")

    def install(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(update_cli, "_find_project_dir", lambda: tmp_path)
        monkeypatch.setattr(update_cli, "_has_local_changes", lambda _d: False)
        monkeypatch.setattr(update_cli, "_git", self.git)
        monkeypatch.setattr(update_cli, "_run", self.run)
        monkeypatch.setattr(update_cli, "_confirm", lambda _p: True)
        # Unverified path: ``_resolve_update_target`` then moves to the
        # branch tip without another git call.
        monkeypatch.setattr(update_cli, "_handle_verification", lambda _skip: (True, None))
        monkeypatch.setattr(update_cli, "_maybe_migrate_launcher", lambda: None)
        monkeypatch.setattr(update_cli, "_maybe_register_codex", lambda: None)
        monkeypatch.setattr(update_cli, "read_changelog_security", lambda _d: False)
        monkeypatch.setattr(doctor, "main", lambda _argv: self.doctor_rc)


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Harness:
    h = _Harness()
    h.install(monkeypatch, tmp_path)
    return h


# ---------------------------------------------------------------------------
# The control: everything healthy
# ---------------------------------------------------------------------------


def test_healthy_update_reports_complete_and_exits_0(
    harness: _Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = update_cli.main([])
    out = capsys.readouterr()

    assert rc == 0
    assert "Update complete." in out.out
    assert "post-update checks failed" not in out.err
    # The shim refresh really did run — the other tests are about its result.
    assert any(cmd[:3] == ["uv", "tool", "install"] for cmd in harness.ran)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_doctor_failure_is_not_swallowed(
    harness: _Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    """An install that fails its own health check is not a complete update."""
    harness.doctor_rc = 1

    rc = update_cli.main([])
    out = capsys.readouterr()

    assert rc == 1
    assert "doctor" in out.err
    assert "Update complete." not in out.out


def test_doctor_failure_counts_even_when_the_test_suite_passed(
    harness: _Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.tests_rc = 0
    harness.doctor_rc = 1

    assert update_cli.main([]) == 1
    assert "tests are failing" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# CLI shim refresh
# ---------------------------------------------------------------------------


def test_shim_refresh_failure_is_reported_and_fails_the_run(
    harness: _Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    """`uv tool install` resolves from pyproject, so it is the only step that
    exercises the dependency set a fresh install gets."""
    harness.shim_rc = 1
    harness.shim_stderr = "No solution found when resolving dependencies"

    rc = update_cli.main([])
    out = capsys.readouterr()

    assert rc == 1
    assert "CLI shim refresh" in out.err
    assert "No solution found when resolving dependencies" in out.err
    assert "Update complete." not in out.out


def test_shim_failure_says_the_path_command_may_be_stale(
    harness: _Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.shim_rc = 2

    update_cli.main([])

    assert "on your PATH" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Test suite: existing behaviour must survive
# ---------------------------------------------------------------------------


def test_failing_test_suite_still_prints_the_rollback_hint(
    harness: _Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.tests_rc = 1

    rc = update_cli.main([])
    out = capsys.readouterr()

    assert rc == 1
    assert "reset --hard aaaaaaa" in out.out
    assert "test suite" in out.err


def test_every_failed_check_is_named_in_one_verdict(
    harness: _Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.tests_rc = 1
    harness.shim_rc = 1
    harness.doctor_rc = 1

    rc = update_cli.main([])
    err = capsys.readouterr().err

    assert rc == 1
    for name in ("test suite", "CLI shim refresh", "doctor"):
        assert name in err


# ---------------------------------------------------------------------------
# Paths that must keep short-circuiting before the post-update checks
# ---------------------------------------------------------------------------


def test_already_up_to_date_exits_0_without_running_checks(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def same_commit(project_dir: Path, *args: str, check: bool = True) -> Any:
        if args == ("rev-parse", "origin/main"):
            return _completed(stdout="aaaaaaa\n")
        return harness.git(project_dir, *args, check=check)

    monkeypatch.setattr(update_cli, "_git", same_commit)
    harness.doctor_rc = 1  # would fail the verdict if the checks ran

    rc = update_cli.main([])

    assert rc == 0
    assert "Already up to date." in capsys.readouterr().out
    assert harness.ran == []


def test_declining_the_prompt_exits_0_without_running_checks(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(update_cli, "_confirm", lambda _p: False)
    harness.doctor_rc = 1

    rc = update_cli.main([])

    assert rc == 0
    assert "Aborted." in capsys.readouterr().out
    assert harness.ran == []
