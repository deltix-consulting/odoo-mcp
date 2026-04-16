"""Tests for the TOML config loader."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from odoo_mcp.config import load_config
from odoo_mcp.errors import ConfigError


def _write_cfg(path: Path, body: str, mode: int = 0o600) -> Path:
    path.write_text(body)
    if os.name == "posix":
        path.chmod(mode)
    return path


_VALID_CONFIG = """
[defaults]
timeout_seconds = 30
max_records_default = 50
max_records_hard_cap = 500

[instances.dev]
url = "https://dev.example.odoo.com"
database = "dev_db"
credentials_env_prefix = "ODOO_MCP_DEV"
production = false

[instances.prod]
url = "https://example.odoo.com"
database = "prod_db"
credentials_env_prefix = "ODOO_MCP_PROD"
production = true
"""


def test_load_valid_config(tmp_path: Path) -> None:
    cfg_file = _write_cfg(tmp_path / "config.toml", _VALID_CONFIG)
    cfg = load_config(cfg_file)
    assert "dev" in cfg.instances
    assert "prod" in cfg.instances
    assert cfg.instances["prod"].production is True
    assert cfg.instances["dev"].rate_limit_per_minute == 300  # default dev
    assert cfg.instances["prod"].rate_limit_per_minute == 60  # default prod


@pytest.mark.skipif(os.name != "posix", reason="chmod-based check")
def test_load_rejects_loose_permissions(tmp_path: Path) -> None:
    cfg_file = _write_cfg(tmp_path / "config.toml", _VALID_CONFIG, mode=0o644)
    with pytest.raises(ConfigError, match="loose permissions"):
        load_config(cfg_file)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


def test_malformed_toml(tmp_path: Path) -> None:
    cfg_file = _write_cfg(tmp_path / "config.toml", "not = valid = toml")
    with pytest.raises(ConfigError, match="Could not parse"):
        load_config(cfg_file)


def test_unknown_top_level_key_rejected(tmp_path: Path) -> None:
    body = _VALID_CONFIG + '\n[instances.staging]\nurl="https://s"\ndatabase="s"\ncredentials_env_prefix="X"\nweird_key=true\n'
    cfg_file = _write_cfg(tmp_path / "config.toml", body)
    with pytest.raises(ConfigError, match="Unknown keys"):
        load_config(cfg_file)


def test_http_not_allowed_on_prod(tmp_path: Path) -> None:
    body = """
[instances.prod]
url = "http://prod.example.com"
database = "p"
credentials_env_prefix = "ODOO_MCP_PROD"
production = true
"""
    cfg_file = _write_cfg(tmp_path / "config.toml", body)
    with pytest.raises(ConfigError, match="HTTPS"):
        load_config(cfg_file)


def test_allow_self_signed_rejected_on_prod(tmp_path: Path) -> None:
    body = """
[instances.prod]
url = "https://prod.example.com"
database = "p"
credentials_env_prefix = "ODOO_MCP_PROD"
production = true
allow_self_signed = true
"""
    cfg_file = _write_cfg(tmp_path / "config.toml", body)
    with pytest.raises(ConfigError, match="allow_self_signed"):
        load_config(cfg_file)


def test_missing_required_field(tmp_path: Path) -> None:
    body = """
[instances.dev]
url = "https://dev.example.com"
credentials_env_prefix = "X"
"""
    cfg_file = _write_cfg(tmp_path / "config.toml", body)
    with pytest.raises(ConfigError, match="database"):
        load_config(cfg_file)


def test_no_instances_refused(tmp_path: Path) -> None:
    cfg_file = _write_cfg(tmp_path / "config.toml", "[defaults]\ntimeout_seconds = 30\n")
    with pytest.raises(ConfigError, match="No \\[instances"):
        load_config(cfg_file)
