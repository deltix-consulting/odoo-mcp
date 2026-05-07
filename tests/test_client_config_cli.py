"""Tests for the ``odoo-mcp client-config`` CLI helper."""

from __future__ import annotations

import json
from io import StringIO
from unittest.mock import patch

import pytest

from odoo_mcp import client_config_cli


def _run(argv: list[str]) -> tuple[int, str, str]:
    """Run the CLI with stdout/stderr captured."""
    stdout = StringIO()
    stderr = StringIO()
    with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
        rc = client_config_cli.main(argv)
    return rc, stdout.getvalue(), stderr.getvalue()


def test_list_prints_every_supported_client() -> None:
    rc, out, _ = _run(["--list"])
    assert rc == 0
    for name, _desc in client_config_cli._SUPPORTED_CLIENTS:
        assert name in out


def test_specific_client_prints_only_that_block() -> None:
    rc, out, _ = _run(["--client", "claude-desktop"])
    assert rc == 0
    assert "claude_desktop_config.json" in out
    # Cursor's marker shouldn't appear in a Claude-only output.
    assert ".cursor/mcp.json" not in out


def test_no_args_prints_all_clients() -> None:
    rc, out, _ = _run([])
    assert rc == 0
    # Each client's description string should appear somewhere.
    for _name, desc in client_config_cli._SUPPORTED_CLIENTS:
        assert desc in out


def test_unknown_client_rejected_by_argparse() -> None:
    # argparse exits with code 2 on bad choices.
    with pytest.raises(SystemExit) as exc:
        _run(["--client", "emacs"])
    assert exc.value.code == 2


def test_claude_desktop_block_is_valid_json() -> None:
    rc, out, _ = _run(["--client", "claude-desktop"])
    assert rc == 0
    # Find the JSON object in the output (everything between the first { and
    # its matching close brace at the same indent level).
    start = out.index("{")
    end = out.rindex("}") + 1
    body = json.loads(out[start:end])
    assert "mcpServers" in body
    assert "odoo-mcp" in body["mcpServers"]
    assert body["mcpServers"]["odoo-mcp"]["args"] == ["launch"]


def test_codex_block_is_toml_shape() -> None:
    rc, out, _ = _run(["--client", "codex"])
    assert rc == 0
    assert "[mcp_servers.odoo-mcp]" in out
    assert 'args = ["launch"]' in out


def test_cursor_block_mentions_cursor_path() -> None:
    rc, out, _ = _run(["--client", "cursor"])
    assert rc == 0
    assert ".cursor/mcp.json" in out


def test_zed_block_uses_context_servers_key() -> None:
    rc, out, _ = _run(["--client", "zed"])
    assert rc == 0
    start = out.index("{")
    end = out.rindex("}") + 1
    body = json.loads(out[start:end])
    assert "context_servers" in body


def test_continue_block_uses_experimental_key() -> None:
    rc, out, _ = _run(["--client", "continue"])
    assert rc == 0
    assert "experimental" in out
    assert "modelContextProtocolServers" in out


def test_generic_stdio_block_has_command() -> None:
    rc, out, _ = _run(["--client", "generic-stdio"])
    assert rc == 0
    assert "launch" in out


def test_warning_when_odoo_mcp_not_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_config_cli.shutil, "which", lambda _: None)
    rc, _out, err = _run(["--client", "claude-desktop"])
    assert rc == 0
    assert "PATH" in err


def test_block_for_unknown_client_raises() -> None:
    with pytest.raises(ValueError, match="Unknown client"):
        client_config_cli._block_for("emacs", "/usr/bin/odoo-mcp")
