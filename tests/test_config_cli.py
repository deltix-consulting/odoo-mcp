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

# Every per-instance knob the loader accepts, set to a non-default value. The
# point of the fixture is that ``config show`` must not be able to stay silent
# about any of them.
_OVERRIDDEN_BODY = """
[defaults]
timeout_seconds = 30
max_records_default = 50
max_records_hard_cap = 500
rotation_warning_days = 90

[instances.prod]
url = "https://deltix.odoo.com"
database = "deltix"
credentials_env_prefix = "ODOO_MCP_PROD"
production = true
max_records_default = 200
max_records_hard_cap = 5000
refuse_admin_on_production = false
external_comms_enabled = true
max_commits_per_unlock = 250
unlock_ttl_seconds = 3600
custom_sensitive_field_patterns = ["x_loonfiche.*"]
attachment_source_paths = ["/var/run/odoo-mcp/inbox"]

[instances.prod.smart_fields_overrides]
"res.partner" = ["name", "vat"]

[instances.prod.sensitive_fields]
"hr.employee" = ["ssnid"]
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


def test_show_reports_per_instance_record_caps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A cap overridden on the instance must not read as the global default.

    The Defaults block prints the global pair, so an instance block that omits
    its own values doesn't read as "unset" — it reads as "500 applies here",
    which is the wrong answer whenever the instance overrides it.
    """
    path = _write_config(tmp_path, _OVERRIDDEN_BODY)
    monkeypatch.setattr("odoo_mcp.config_cli.load_config", lambda _p=None: _load(path))
    monkeypatch.setattr("odoo_mcp.config_cli._keychain_get", lambda _n, _s: "fake_value")

    rc = config_cli.main(["show"])
    assert rc == 0
    out = capsys.readouterr().out
    # Global block keeps the defaults ...
    assert "max_records_hard_cap:  500" in out
    # ... and the instance block carries the override that actually applies.
    assert "max_records_default:     200" in out
    assert "max_records_hard_cap:    5000" in out


def test_show_reports_security_tunables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The settings that decide what the MCP may do must be in the dump."""
    path = _write_config(tmp_path, _OVERRIDDEN_BODY)
    monkeypatch.setattr("odoo_mcp.config_cli.load_config", lambda _p=None: _load(path))
    monkeypatch.setattr("odoo_mcp.config_cli._keychain_get", lambda _n, _s: "fake_value")

    rc = config_cli.main(["show"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "refuse_admin_on_production: false" in out
    assert "external_comms_enabled:  true" in out
    assert "max_commits_per_unlock:  250" in out
    assert "unlock_ttl_seconds:      3600" in out
    assert "/var/run/odoo-mcp/inbox" in out
    assert "x_loonfiche.*" in out
    assert "res.partner: [name, vat]" in out


def test_show_defaults_at_rest_name_their_safe_stance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unset tunable is still a policy — print it rather than omit it."""
    path = _write_config(tmp_path, _VALID_BODY)
    monkeypatch.setattr("odoo_mcp.config_cli.load_config", lambda _p=None: _load(path))
    monkeypatch.setattr("odoo_mcp.config_cli._keychain_get", lambda _n, _s: "fake_value")

    rc = config_cli.main(["show"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "refuse_admin_on_production: true" in out
    assert "external_comms_enabled:  false" in out
    assert "attachment_source_paths: (none, source_path refused)" in out
    assert "custom_sensitive_field_patterns: (none)" in out
    assert "smart_fields_overrides:   (none, using built-in smart defaults)" in out


# Fields rendered under a different label than their dataclass attribute name.
_LABEL_ALIASES = {
    "name": "Instance: ",
    "sensitive_fields": "sensitive_fields_override",
    "audit_log": "Audit log:",
}


def test_show_renders_every_configurable_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No config key may be silently dropped from the dump.

    Guards the actual defect class: the tunables added after ``config show``
    was written (prod-write window, external comms, attachment paths, custom
    redaction patterns) were never added to the renderer, so the dump quietly
    described a config the loader wasn't using. This fails the moment a new
    field lands on either dataclass without a matching line.
    """
    import dataclasses

    from odoo_mcp.config import Defaults, InstanceConfig

    path = _write_config(tmp_path, _OVERRIDDEN_BODY)
    monkeypatch.setattr("odoo_mcp.config_cli.load_config", lambda _p=None: _load(path))
    monkeypatch.setattr("odoo_mcp.config_cli._keychain_get", lambda _n, _s: "fake_value")

    rc = config_cli.main(["show"])
    assert rc == 0
    out = capsys.readouterr().out

    missing = [
        f.name
        for dc in (Defaults, InstanceConfig)
        for f in dataclasses.fields(dc)
        if _LABEL_ALIASES.get(f.name, f.name) not in out
    ]
    assert not missing, f"config show omits: {missing}"


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
