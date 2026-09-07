"""An unusable L2 cache file must not take the server or the CLI down.

The persistent fields cache is a performance optimization holding field
*metadata* only — no secrets, no record values — and "no L2" is already a
supported runtime state (``fields_cache_path = ""``). Before this was fixed,
a cache file the filesystem handed back in an unusable shape (truncated by a
crash, replaced by a directory, mode 000) raised out of
``PersistentFieldsCache.__init__`` and:

* ``build_app`` — every tool, every instance — refused to start;
* ``odoo-mcp status`` tracebacked (it only catches ``OdooMcpError``);
* ``odoo-mcp cache --info`` tracebacked;
* ``odoo-mcp cache --clear`` — the documented way out — tracebacked before
  it could clear anything.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from odoo_mcp import cache_cli, status_cli
from odoo_mcp.server import build_app


def _unusable_cache(tmp_path: Path) -> Path:
    """A file at the cache path that SQLite cannot open at all."""
    path = tmp_path / "fields-cache.db"
    path.write_bytes(b"SQLite format 3\x00 ...cut off mid-write by a crash")
    return path


def _config(tmp_path: Path, cache_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        textwrap.dedent(
            f"""
            [defaults]
            audit_log = "{tmp_path / "audit.jsonl"}"
            fields_cache_path = "{cache_path}"

            [instances.dev]
            url = "https://example.odoo.com"
            database = "db"
            credentials_env_prefix = "ODOO_MCP_DEV"
            production = false
            rate_limit_per_minute = 100
            allowed_models = ["*"]
            """
        )
    )
    cfg.chmod(0o600)
    return cfg


def test_build_app_starts_with_an_unusable_cache_file(tmp_path: Path) -> None:
    app = build_app(_config(tmp_path, _unusable_cache(tmp_path)))
    # The server is up and the instance is usable...
    assert set(app.instances) == {"dev"}
    # ...it just has no L2, exactly as if the operator had disabled it.
    assert app.instances["dev"].client._persistent_fields_cache is None


def test_build_app_still_wires_the_cache_when_the_file_is_healthy(tmp_path: Path) -> None:
    """The degrade path must not swallow the healthy one."""
    app = build_app(_config(tmp_path, tmp_path / "fresh-cache.db"))
    assert app.instances["dev"].client._persistent_fields_cache is not None


def test_status_does_not_traceback_on_an_unusable_cache_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _config(tmp_path, _unusable_cache(tmp_path))
    monkeypatch.setattr(status_cli, "build_app", lambda: build_app(cfg))
    assert status_cli.main([]) == 0
    assert "dev" in capsys.readouterr().out


def test_cache_info_reports_an_unusable_file_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = _unusable_cache(tmp_path)
    monkeypatch.setattr(cache_cli, "_resolve_cache_path", lambda: bad)
    assert cache_cli.main(["--info"]) == 1
    err = capsys.readouterr().err
    assert str(bad) in err
    assert "rm " in err  # the recovery command, not just the symptom


def test_cache_clear_reports_an_unusable_file_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--clear`` is the way out of a broken cache; it must not be the casualty."""
    bad = _unusable_cache(tmp_path)
    monkeypatch.setattr(cache_cli, "_resolve_cache_path", lambda: bad)
    assert cache_cli.main(["--clear"]) == 1
    assert str(bad) in capsys.readouterr().err
