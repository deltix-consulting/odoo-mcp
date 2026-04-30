"""Tests for the ``python -m odoo_mcp`` dispatch in __main__.py.

Specifically the v0.7.0 ``launch`` subcommand which loads Keychain
credentials into ``os.environ`` and then drops into ``server.run()``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


def test_launch_loads_env_then_runs_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`python -m odoo_mcp launch` must populate os.environ before server.run."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[defaults]\n"
        "timeout_seconds = 30\n"
        "\n"
        "[instances.main]\n"
        'url = "https://example.odoo.com"\n'
        'database = "db"\n'
        'credentials_env_prefix = "ODOO_MCP_MAIN"\n'
        "production = false\n"
    )
    # _collect_launch_env now refuses loose perms (v0.8.0); match the
    # 0o600 mode the wizard writes in production.
    cfg.chmod(0o600)

    from odoo_mcp import setup_wizard

    monkeypatch.setattr(setup_wizard, "DEFAULT_CONFIG_PATH", cfg)

    # Fake Keychain reads.
    def fake_keychain_get(_name: str, service: str) -> str:
        if service.endswith("_USERNAME"):
            return "alice@example.com"
        if service.endswith("_API_KEY"):
            return "supersecret"
        return ""

    monkeypatch.setattr(setup_wizard, "_keychain_get", fake_keychain_get)

    # Capture os.environ at the moment server.run is called.
    captured: dict[str, str] = {}

    async def fake_run() -> None:
        import os as _os

        captured.update(_os.environ)

    import odoo_mcp.server as server_mod

    monkeypatch.setattr(server_mod, "run", fake_run)

    monkeypatch.setattr(sys, "argv", ["odoo-mcp", "launch"])

    from odoo_mcp.__main__ import main as main_entry

    rc = main_entry()
    assert rc == 0
    assert captured.get("ODOO_MCP_MAIN_USERNAME") == "alice@example.com"
    assert captured.get("ODOO_MCP_MAIN_API_KEY") == "supersecret"


def test_launch_help_listed_in_main_doc() -> None:
    """The new ``launch`` subcommand is documented in __main__'s docstring."""
    import odoo_mcp.__main__ as m

    assert m.__doc__ is not None
    assert "launch" in m.__doc__
    assert "uninstall" in m.__doc__
