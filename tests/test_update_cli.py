"""Tests for ``odoo-mcp update`` helpers — currently focused on the
legacy-launcher migration path that runs after a successful ``git pull``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from odoo_mcp import update_cli


def _wire_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    """Redirect ``setup_wizard._LAUNCH_SH`` and ``_CLAUDE_DESKTOP_CONFIG`` into tmp."""
    from odoo_mcp import setup_wizard

    launch_sh = tmp_path / "launch.sh"
    cd_config = tmp_path / "claude_desktop_config.json"
    monkeypatch.setattr(setup_wizard, "_LAUNCH_SH", launch_sh)
    monkeypatch.setattr(setup_wizard, "_CLAUDE_DESKTOP_CONFIG", cd_config)
    return launch_sh, cd_config


def _write_legacy_config(cd_config: Path, launch_sh: Path) -> None:
    cd_config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "odoo-mcp": {
                        "command": str(launch_sh),
                        "args": [],
                    },
                    "other-mcp": {"command": "/usr/bin/somethingelse"},
                }
            },
            indent=2,
        )
    )


def test_migration_rewrites_claude_desktop_config_before_deleting_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    launch_sh, cd_config = _wire_paths(monkeypatch, tmp_path)
    launch_sh.write_text("#!/bin/bash\n# legacy launcher\n")
    _write_legacy_config(cd_config, launch_sh)

    # Stub _resolve_odoo_mcp_command so the rewrite picks a stable path
    # without depending on whether `odoo-mcp` is on the test runner's PATH.
    from odoo_mcp import setup_wizard

    monkeypatch.setattr(
        setup_wizard, "_resolve_odoo_mcp_command", lambda: "/usr/local/bin/odoo-mcp"
    )

    update_cli._maybe_migrate_launcher()

    # Script must be gone.
    assert not launch_sh.exists()

    # Config must point at odoo-mcp launch directly, not launch.sh.
    rewritten = json.loads(cd_config.read_text())
    entry = rewritten["mcpServers"]["odoo-mcp"]
    assert "launch.sh" not in entry["command"]
    assert entry["command"].endswith("odoo-mcp")
    assert entry["args"] == ["launch"]
    # Other MCPs must be preserved untouched.
    assert rewritten["mcpServers"]["other-mcp"] == {"command": "/usr/bin/somethingelse"}

    out = capsys.readouterr().out
    assert "Migrated launcher" in out
    assert "registers" in out


def test_migration_substring_match_handles_alternate_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A config command that references launch.sh via a different (e.g.
    symlink-resolved) path should still trigger a rewrite."""
    launch_sh, cd_config = _wire_paths(monkeypatch, tmp_path)
    launch_sh.write_text("#!/bin/bash\n")
    # Different absolute path that still ends in launch.sh.
    cd_config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "odoo-mcp": {
                        "command": "/var/legacy/somewhere/launch.sh",
                        "args": [],
                    }
                }
            }
        )
    )

    from odoo_mcp import setup_wizard

    monkeypatch.setattr(
        setup_wizard, "_resolve_odoo_mcp_command", lambda: "/usr/local/bin/odoo-mcp"
    )

    update_cli._maybe_migrate_launcher()

    assert not launch_sh.exists()
    rewritten = json.loads(cd_config.read_text())
    assert "launch.sh" not in rewritten["mcpServers"]["odoo-mcp"]["command"]


def test_migration_aborts_if_rewrite_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    launch_sh, cd_config = _wire_paths(monkeypatch, tmp_path)
    launch_sh.write_text("#!/bin/bash\n# legacy\n")
    _write_legacy_config(cd_config, launch_sh)

    original_config = cd_config.read_text()

    from odoo_mcp import setup_wizard

    def _boom() -> None:
        raise OSError("disk full")

    monkeypatch.setattr(setup_wizard, "_register_claude_desktop", _boom)

    update_cli._maybe_migrate_launcher()

    # Script MUST still be present — better stale wrapper than broken config.
    assert launch_sh.exists()
    # Config must be unchanged (we never even attempted to write it).
    assert cd_config.read_text() == original_config

    out = capsys.readouterr().out
    assert "ERROR" in out
    assert "left in place" in out


def test_migration_handles_already_migrated_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """If the config already points at 'odoo-mcp launch' directly but a
    stale launch.sh remains, the migration should warn and do nothing —
    it must not delete the script (the user may have deliberately kept
    it) and must not rewrite an already-correct config."""
    launch_sh, cd_config = _wire_paths(monkeypatch, tmp_path)
    launch_sh.write_text("#!/bin/bash\n# orphaned legacy script\n")
    cd_config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "odoo-mcp": {
                        "command": "/usr/local/bin/odoo-mcp",
                        "args": ["launch"],
                    }
                }
            }
        )
    )
    config_before = cd_config.read_text()

    rewrite_called = {"hit": False}

    from odoo_mcp import setup_wizard

    def _track_rewrite() -> None:
        rewrite_called["hit"] = True

    monkeypatch.setattr(setup_wizard, "_register_claude_desktop", _track_rewrite)

    update_cli._maybe_migrate_launcher()

    # Script preserved.
    assert launch_sh.exists()
    # Config untouched.
    assert cd_config.read_text() == config_before
    # Rewrite never attempted.
    assert rewrite_called["hit"] is False

    out = capsys.readouterr().out
    assert "Warning" in out
    assert "no matching Claude" in out


def test_migration_no_op_when_launch_sh_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    launch_sh, cd_config = _wire_paths(monkeypatch, tmp_path)
    # No launch.sh, no config — nothing to do, no output.
    assert not launch_sh.exists()
    assert not cd_config.exists()

    from odoo_mcp import setup_wizard

    rewrite_calls: list[Any] = []
    monkeypatch.setattr(
        setup_wizard,
        "_register_claude_desktop",
        lambda: rewrite_calls.append(None),
    )

    update_cli._maybe_migrate_launcher()

    assert rewrite_calls == []
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# `odoo-mcp update --check` must not lie when the fetch fails.
# Real-world failure: v0.15.10 user behind a NAT hit the anonymous
# GitHub rate-limit; --check returned None and the old code printed
# "Up to date (version 0.15.10)" — hiding the very failure that made
# the verified-update path insecure. The fix distinguishes
# "unreachable" from "no newer release".
# ---------------------------------------------------------------------------


def test_print_check_reports_unreachable_when_fetch_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(update_cli, "fetch_latest_tag", lambda: None)
    rc = update_cli._print_check("0.15.10")
    captured = capsys.readouterr()
    # Non-zero exit so scripts / CI surface the failure.
    assert rc == 1
    # The misleading "Up to date" line must not appear anywhere.
    assert "Up to date" not in captured.out
    assert "Up to date" not in captured.err
    # The real reason goes to stderr — clearly stated, no warning theater.
    assert "Could not reach GitHub" in captured.err
    assert "0.15.10" in captured.err


def test_print_check_reports_up_to_date_when_current(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(update_cli, "fetch_latest_tag", lambda: "v0.17.4")
    monkeypatch.setattr(update_cli, "check_for_update", lambda _v: None)
    rc = update_cli._print_check("0.17.4")
    out = capsys.readouterr().out
    assert rc == 0
    assert "Up to date (version 0.17.4)" in out


def test_print_check_reports_update_available(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(update_cli, "fetch_latest_tag", lambda: "v0.17.4")
    monkeypatch.setattr(update_cli, "check_for_update", lambda _v: ("0.15.10", "0.17.4"))
    rc = update_cli._print_check("0.15.10")
    out = capsys.readouterr().out
    assert rc == 0
    assert "Update available: 0.17.4" in out
    assert "0.15.10" in out
