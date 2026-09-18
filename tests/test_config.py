"""Tests for the TOML config loader."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from odoo_mcp.config import load_config
from odoo_mcp.errors import ConfigError
from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD


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
    body = (
        _VALID_CONFIG
        + '\n[instances.staging]\nurl="https://s"\ndatabase="s"\ncredentials_env_prefix="X"\nweird_key=true\n'
    )
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


def test_sensitive_fields_override_parsed(tmp_path: Path) -> None:
    body = (
        _VALID_CONFIG
        + '\n[instances.dev.sensitive_fields]\n"res.partner" = ["vat", "ref"]\n'
        + '"hr.employee" = []\n'
    )
    cfg_file = _write_cfg(tmp_path / "config.toml", body)
    cfg = load_config(cfg_file)
    dev = cfg.instances["dev"]
    assert dev.sensitive_fields["res.partner"] == frozenset({"vat", "ref"})
    # Explicit empty list: model present with an empty override set.
    assert dev.sensitive_fields["hr.employee"] == frozenset()
    # Omitted instance has an empty map — falls back to global default.
    assert cfg.instances["prod"].sensitive_fields == {}


def test_sensitive_fields_rejects_non_table(tmp_path: Path) -> None:
    body = _VALID_CONFIG + '\n[instances.dev]\nsensitive_fields = "nope"\n'
    # TOML disallows redeclaring the table — use an inline form instead.
    body = _VALID_CONFIG.replace(
        "[instances.dev]",
        '[instances.dev]\nsensitive_fields = "nope"',
    )
    cfg_file = _write_cfg(tmp_path / "config.toml", body)
    with pytest.raises(ConfigError, match="sensitive_fields"):
        load_config(cfg_file)


def test_sensitive_fields_rejects_bad_value_type(tmp_path: Path) -> None:
    body = _VALID_CONFIG.replace(
        "[instances.dev]",
        '[instances.dev]\nsensitive_fields = { "res.partner" = "vat" }',
    )
    cfg_file = _write_cfg(tmp_path / "config.toml", body)
    with pytest.raises(ConfigError, match="must be a list"):
        load_config(cfg_file)


def test_sensitive_fields_rejects_non_string_entries(tmp_path: Path) -> None:
    body = _VALID_CONFIG.replace(
        "[instances.dev]",
        '[instances.dev]\nsensitive_fields = { "res.partner" = [42] }',
    )
    cfg_file = _write_cfg(tmp_path / "config.toml", body)
    with pytest.raises(ConfigError, match="non-empty strings"):
        load_config(cfg_file)


def test_default_is_open_mode(tmp_path: Path) -> None:
    """A minimal config with no allowed_models override gets open mode."""
    cfg_file = _write_cfg(tmp_path / "config.toml", _VALID_CONFIG)
    cfg = load_config(cfg_file)
    # Default defaults.allowed_models is now the wildcard tuple.
    assert cfg.defaults.allowed_models == (ALLOWLIST_WILDCARD,)
    # Each instance inherits it unless overridden.
    for inst in cfg.instances.values():
        assert ALLOWLIST_WILDCARD in inst.allowed_models


def test_explicit_wildcard_allowed(tmp_path: Path) -> None:
    """Users can spell out allowed_models = ['*'] in TOML."""
    body = """
[defaults]
allowed_models = ["*"]

[instances.dev]
url = "https://dev.example.odoo.com"
database = "dev_db"
credentials_env_prefix = "ODOO_MCP_DEV"
production = false
"""
    cfg_file = _write_cfg(tmp_path / "config.toml", body)
    cfg = load_config(cfg_file)
    assert cfg.defaults.allowed_models == (ALLOWLIST_WILDCARD,)
    assert ALLOWLIST_WILDCARD in cfg.instances["dev"].allowed_models


def test_strict_list_still_works(tmp_path: Path) -> None:
    """Pre-v0.4 behavior: a concrete strict list stays strict."""
    body = """
[defaults]
allowed_models = ["res.partner", "crm.lead"]

[instances.dev]
url = "https://dev.example.odoo.com"
database = "dev_db"
credentials_env_prefix = "ODOO_MCP_DEV"
production = false
"""
    cfg_file = _write_cfg(tmp_path / "config.toml", body)
    cfg = load_config(cfg_file)
    assert cfg.defaults.allowed_models == ("res.partner", "crm.lead")
    dev_models = cfg.instances["dev"].allowed_models
    assert dev_models == frozenset({"res.partner", "crm.lead"})
    assert ALLOWLIST_WILDCARD not in dev_models


def test_duplicate_env_prefix_rejected(tmp_path: Path) -> None:
    body = """
[instances.dev]
url = "https://dev.example.com"
database = "dev_db"
credentials_env_prefix = "ODOO_MCP_SHARED"
production = false

[instances.staging]
url = "https://stg.example.com"
database = "stg_db"
credentials_env_prefix = "ODOO_MCP_SHARED"
production = false
"""
    cfg_file = _write_cfg(tmp_path / "config.toml", body)
    with pytest.raises(ConfigError, match="ODOO_MCP_SHARED"):
        load_config(cfg_file)


# ---------------------------------------------------------------------------
# unlock_ttl_seconds / max_commits_per_unlock: per-instance tunables
# ---------------------------------------------------------------------------


def test_unlock_ttl_seconds_defaults_to_fifteen_minutes(tmp_path: Path) -> None:
    """Operators don't have to set anything to get the sensible default.
    Pin the default so an unnoticed refactor to 5 minutes (the old value)
    or something absurd is loud."""
    cfg_file = _write_cfg(tmp_path / "config.toml", _VALID_CONFIG)
    cfg = load_config(cfg_file)
    assert cfg.instances["prod"].unlock_ttl_seconds == 15 * 60


def test_unlock_ttl_seconds_per_instance_override(tmp_path: Path) -> None:
    """Operators can tune the initial unlock window per instance. A
    high-throughput batch tenant sets it up (60 min max); an SOX-strict
    tenant drops it (60s min). This is the whole point of making it
    configurable."""
    body = _VALID_CONFIG + "\nunlock_ttl_seconds = 1800\n"  # 30 min on prod
    cfg_file = _write_cfg(tmp_path / "config.toml", body)
    cfg = load_config(cfg_file)
    assert cfg.instances["prod"].unlock_ttl_seconds == 1800


def test_unlock_ttl_seconds_below_bound_refused(tmp_path: Path) -> None:
    """Sub-60s TTL is operationally unusable — the dry-run review takes
    longer than that. Config parser refuses the value rather than
    silently promoting it, so a misconfigured tenant sees the error at
    startup, not on the first blocked write."""
    body = _VALID_CONFIG + "\nunlock_ttl_seconds = 10\n"
    cfg_file = _write_cfg(tmp_path / "config.toml", body)
    with pytest.raises(ConfigError, match="unlock_ttl_seconds"):
        load_config(cfg_file)


def test_unlock_ttl_seconds_above_bound_refused(tmp_path: Path) -> None:
    """Above 3600s (1 hour) defeats the operator-in-the-loop pattern
    the unlock exists to enforce. Refuse loudly rather than accepting
    a value that quietly weakens the security posture."""
    body = _VALID_CONFIG + "\nunlock_ttl_seconds = 7200\n"  # 2 hours
    cfg_file = _write_cfg(tmp_path / "config.toml", body)
    with pytest.raises(ConfigError, match="unlock_ttl_seconds"):
        load_config(cfg_file)


def test_max_commits_per_unlock_defaults_to_fifty(tmp_path: Path) -> None:
    """Default bumped from 10 → 50 in v0.27.0. The payload-digest
    binding (v0.18.0) already binds each commit to its previewed
    content, so a larger burst budget doesn't weaken the operator's
    approval — it just unblocks batch flows that used to hit the cap
    mid-run and force an unnecessary re-unlock."""
    cfg_file = _write_cfg(tmp_path / "config.toml", _VALID_CONFIG)
    cfg = load_config(cfg_file)
    assert cfg.instances["prod"].max_commits_per_unlock == 50


def test_language_defaults_to_en_us(tmp_path: Path) -> None:
    """No language configured anywhere -> en_US, preserving prior behavior."""
    cfg_file = _write_cfg(tmp_path / "config.toml", _VALID_CONFIG)
    cfg = load_config(cfg_file)
    assert cfg.defaults.language == "en_US"
    assert cfg.instances["dev"].language == "en_US"
    assert cfg.instances["prod"].language == "en_US"


def test_language_default_propagates_to_instances(tmp_path: Path) -> None:
    """A [defaults].language flows to instances that don't override it."""
    body = """
[defaults]
language = "nl_BE"

[instances.dev]
url = "https://dev.example.odoo.com"
database = "dev_db"
credentials_env_prefix = "ODOO_MCP_DEV"
production = false
"""
    cfg_file = _write_cfg(tmp_path / "config.toml", body)
    cfg = load_config(cfg_file)
    assert cfg.defaults.language == "nl_BE"
    assert cfg.instances["dev"].language == "nl_BE"


def test_per_instance_language_overrides_default(tmp_path: Path) -> None:
    body = """
[defaults]
language = "nl_BE"

[instances.dev]
url = "https://dev.example.odoo.com"
database = "dev_db"
credentials_env_prefix = "ODOO_MCP_DEV"
production = false

[instances.fr]
url = "https://fr.example.odoo.com"
database = "fr_db"
credentials_env_prefix = "ODOO_MCP_FR"
production = false
language = "fr_FR"
"""
    cfg_file = _write_cfg(tmp_path / "config.toml", body)
    cfg = load_config(cfg_file)
    assert cfg.instances["dev"].language == "nl_BE"
    assert cfg.instances["fr"].language == "fr_FR"


def test_invalid_language_rejected(tmp_path: Path) -> None:
    body = """
[instances.dev]
url = "https://dev.example.odoo.com"
database = "dev_db"
credentials_env_prefix = "ODOO_MCP_DEV"
production = false
language = "not a locale"
"""
    cfg_file = _write_cfg(tmp_path / "config.toml", body)
    with pytest.raises(ConfigError, match="locale code"):
        load_config(cfg_file)
