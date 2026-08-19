"""An unreadable config must surface as ConfigError, not as a traceback.

``load_config`` documents "Raises :class:`ConfigError` on any problem", and
four call sites are written against that contract:

* ``doctor`` — renders ``✗ Load config`` and (with ``--json``) a machine
  readable ``{"ok": false, ...}`` object.
* ``config show`` / ``config validate`` — print ``ConfigError: ...`` to
  stderr and exit 1.
* ``cache`` — falls back to the default cache path, because that CLI
  "must work even if the user has a broken config (typo, bad permissions)".

Before this fix the loader let ``OSError`` escape for the one case its own
permission gate accepts — a file whose mode is ``000`` (no group/other bits,
so ``_check_file_permissions`` passes) or a file inside an unsearchable
directory. Every one of those four handlers was bypassed by a raw
``PermissionError`` traceback, and ``doctor --json`` printed nothing at all
on stdout, so a monitoring script got no verdict instead of a failed one.

The sibling half of the same control already fails this way round:
``AuditLog._open`` wraps ``OSError`` into ``AuditLogError``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from odoo_mcp import cache_cli, config_cli
from odoo_mcp.config import DEFAULT_FIELDS_CACHE, load_config
from odoo_mcp.doctor import run_doctor
from odoo_mcp.errors import ConfigError

# ``chmod 000`` does not restrain root, so the negative tests are only
# meaningful for an unprivileged posix user.
_needs_unprivileged_posix = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="chmod-based check requires an unprivileged posix user",
)

_VALID_CONFIG = """
[instances.dev]
url = "https://dev.example.odoo.com"
database = "dev_db"
credentials_env_prefix = "ODOO_MCP_DEV"
allowed_models = ["res.partner"]
"""


def _write_cfg(tmp_path: Path) -> Path:
    cfg_dir = tmp_path / "cfgdir"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "config.toml"
    cfg_file.write_text(_VALID_CONFIG)
    cfg_file.chmod(0o600)
    return cfg_file


@_needs_unprivileged_posix
def test_unreadable_config_file_raises_config_error(tmp_path: Path) -> None:
    cfg_file = _write_cfg(tmp_path)
    cfg_file.chmod(0o000)
    try:
        with pytest.raises(ConfigError, match="Cannot read config file"):
            load_config(cfg_file)
    finally:
        cfg_file.chmod(0o600)


@_needs_unprivileged_posix
def test_unsearchable_config_dir_raises_config_error(tmp_path: Path) -> None:
    """The EACCES arrives from ``Path.exists()``, before ``open()`` is reached."""
    cfg_file = _write_cfg(tmp_path)
    cfg_file.parent.chmod(0o000)
    try:
        with pytest.raises(ConfigError, match="Cannot read config file"):
            load_config(cfg_file)
    finally:
        cfg_file.parent.chmod(0o700)


@_needs_unprivileged_posix
def test_mode_000_is_not_rejected_as_loose_permissions(tmp_path: Path) -> None:
    """Pins *why* this case escaped: the permission gate accepts mode 000.

    ``0o000 & 0o077 == 0``, so ``_check_file_permissions`` sees nothing
    wrong and the failure lands on the read instead.
    """
    cfg_file = _write_cfg(tmp_path)
    cfg_file.chmod(0o000)
    try:
        with pytest.raises(ConfigError) as excinfo:
            load_config(cfg_file)
    finally:
        cfg_file.chmod(0o600)
    assert "loose permissions" not in str(excinfo.value)


@_needs_unprivileged_posix
def test_doctor_json_reports_a_verdict_for_an_unreadable_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``doctor --json`` must emit a parseable failed verdict, not a traceback."""
    cfg_file = _write_cfg(tmp_path)
    cfg_file.chmod(0o000)
    try:
        rc = run_doctor(cfg_file, as_json=True)
    finally:
        cfg_file.chmod(0o600)
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["steps"][0]["name"] == "Load config"
    assert payload["steps"][0]["ok"] is False
    assert "Cannot read config file" in payload["steps"][0]["detail"]


@_needs_unprivileged_posix
def test_config_validate_reports_unreadable_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg_file = _write_cfg(tmp_path)
    cfg_file.chmod(0o000)
    try:
        rc = config_cli.main(["validate", str(cfg_file)])
    finally:
        cfg_file.chmod(0o600)
    assert rc == 1
    assert "Cannot read config file" in capsys.readouterr().err


@_needs_unprivileged_posix
def test_cache_path_falls_back_when_config_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cache CLI documents that it survives a bad-permissions config."""
    cfg_file = _write_cfg(tmp_path)
    cfg_file.chmod(0o000)
    monkeypatch.setattr("odoo_mcp.config.DEFAULT_CONFIG_PATH", cfg_file)
    try:
        resolved = cache_cli._resolve_cache_path()
    finally:
        cfg_file.chmod(0o600)
    assert resolved == Path(DEFAULT_FIELDS_CACHE).expanduser()
