"""Tests for the ``odoo-mcp config show|validate`` CLI."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from odoo_mcp import config_cli


def _write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body)
    if os.name == "posix":
        path.chmod(0o600)
    return path


_VALID_BODY = """
[defaults]
timeout_seconds = 30
max_records_default = 50
max_records_hard_cap = 500

[instances.prod]
url = "https://deltix.odoo.com"
database = "deltix"
credentials_env_prefix = "ODOO_MCP_PROD"
production = true
"""

_INVALID_BODY = """
[instances.prod]
url = "https://deltix.odoo.com"
database = "deltix"
credentials_env_prefix = "ODOO_MCP_PROD"
unknown_key = "oops"
"""


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def test_show_prints_expected_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_config(tmp_path, _VALID_BODY)
    monkeypatch.setattr("odoo_mcp.config_cli.load_config", lambda _p=None: _load(path))
    # Neutralize the Keychain lookup so the test doesn't depend on macOS state.
    monkeypatch.setattr(
        "odoo_mcp.config_cli._keychain_get",
        lambda _name, _service: "fake_value",
    )

    rc = config_cli.main(["show"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Config file:" in out
    assert "Audit log:" in out
    assert "Defaults" in out
    assert "timeout_seconds:       30" in out
    assert "Instance: prod" in out
    assert "url:                     https://deltix.odoo.com" in out
    assert "database:                deltix" in out
    assert "production:              true" in out
    assert "credentials_env_prefix:  ODOO_MCP_PROD" in out
    assert "credentials_status:      present in Keychain" in out
    assert "sensitive_fields_override: (none, using global defaults)" in out


def test_show_never_prints_credential_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_config(tmp_path, _VALID_BODY)
    monkeypatch.setattr("odoo_mcp.config_cli.load_config", lambda _p=None: _load(path))

    secret = "super_secret_api_key_value_xyz"
    username = "alice@example.com"
    monkeypatch.setattr(
        "odoo_mcp.config_cli._keychain_get",
        lambda name, service: secret if service.endswith("API_KEY") else username,
    )

    rc = config_cli.main(["show"])
    assert rc == 0
    out = capsys.readouterr().out
    # Neither the API key value nor the username are leaked.
    assert secret not in out
    assert username not in out
    # The env-var suffix "ODOO_MCP_PROD_API_KEY" is NOT printed.
    assert "ODOO_MCP_PROD_API_KEY" not in out


def test_show_reports_missing_keychain_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_config(tmp_path, _VALID_BODY)
    monkeypatch.setattr("odoo_mcp.config_cli.load_config", lambda _p=None: _load(path))
    monkeypatch.setattr("odoo_mcp.config_cli._keychain_get", lambda _n, _s: None)

    rc = config_cli.main(["show"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "credentials_status:      missing" in out


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def test_validate_valid_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _write_config(tmp_path, _VALID_BODY)
    rc = config_cli.main(["validate", str(path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Config valid" in out
    assert "prod" in out


def test_validate_invalid_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _write_config(tmp_path, _INVALID_BODY)
    rc = config_cli.main(["validate", str(path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "ConfigError" in err


def test_validate_missing_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "nope.toml"
    rc = config_cli.main(["validate", str(missing)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "ConfigError" in err


def test_no_subcommand_prints_usage(capsys: pytest.CaptureFixture[str]) -> None:
    rc = config_cli.main([])
    assert rc == 2
    err = capsys.readouterr().err
    assert "Usage" in err


def test_unknown_subcommand_prints_usage(capsys: pytest.CaptureFixture[str]) -> None:
    rc = config_cli.main(["bogus"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "Usage" in err


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _load(path: Path) -> object:
    from odoo_mcp.config import load_config

    return load_config(path)
