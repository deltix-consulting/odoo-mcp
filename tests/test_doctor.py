"""Tests for ``odoo-mcp doctor``.

Covers the v0.13.1 fixes:

* B4 — doctor preloads credentials from the credstore so it works
  standalone, not only under ``odoo-mcp launch``.
* F3 — doctor emits a rotation-warning when an instance's API key was
  last set longer ago than ``rotation_warning_days``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from odoo_mcp import doctor


def _write_min_config(tmp_path: Path, *, rotation_days: int | None = None) -> Path:
    """Write a minimal config.toml with one fake instance."""
    cfg = tmp_path / "config.toml"
    audit_log = tmp_path / "audit.jsonl"
    rot_line = f"rotation_warning_days = {rotation_days}\n" if rotation_days is not None else ""
    cfg.write_text(
        "[defaults]\n"
        f'audit_log = "{audit_log}"\n'
        f'fields_cache_path = ""\n'
        f"{rot_line}"
        "\n"
        "[instances.dev]\n"
        'url = "http://example.invalid"\n'
        'database = "db"\n'
        'credentials_env_prefix = "ODOO_MCP_DEV"\n'
        "production = false\n"
    )
    os.chmod(cfg, 0o600)
    return cfg


def _stub_loader(
    monkeypatch: pytest.MonkeyPatch, *, env_to_set: dict[str, str] | None = None
) -> list[None]:
    """Replace setup_wizard.load_credentials_into_os with a stub that records.

    Returns a list whose length tracks the number of times the stub fired.
    """
    calls: list[None] = []

    def fake_load() -> int:
        calls.append(None)
        if env_to_set:
            for k, v in env_to_set.items():
                os.environ[k] = v
        return 0

    from odoo_mcp import setup_wizard

    monkeypatch.setattr(setup_wizard, "load_credentials_into_os", fake_load)
    return calls


def _stub_set_at(monkeypatch: pytest.MonkeyPatch, value: datetime | None) -> None:
    """Replace _credstore.get_secret_set_at to return a fixed datetime."""
    from odoo_mcp import _credstore

    def fake(_instance: str, _service: str) -> datetime | None:
        return value

    monkeypatch.setattr(_credstore, "get_secret_set_at", fake)


# -----------------------------------------------------------------------------
# B4 — doctor preloads credentials from credstore
# -----------------------------------------------------------------------------


def test_doctor_calls_load_credentials_into_os(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _write_min_config(tmp_path)
    calls = _stub_loader(monkeypatch)
    _stub_set_at(monkeypatch, None)
    # The per-instance auth checks will still fail (no real Odoo) — we
    # only care that the credstore preload fired before they ran.
    monkeypatch.delenv("ODOO_MCP_DEV_USERNAME", raising=False)
    monkeypatch.delenv("ODOO_MCP_DEV_API_KEY", raising=False)
    rc = doctor.run_doctor(cfg)
    out = capsys.readouterr().out
    assert calls, "doctor must call load_credentials_into_os at least once"
    # Doctor will fail because creds are still missing — that's fine,
    # the point is the loader ran. Exit code reflects per-instance check
    # failures, not the loader.
    assert rc == 1
    # Per-instance credentials check still surfaces the missing-env error.
    assert "credentials" in out.lower()


def test_doctor_credstore_failure_is_warning_not_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _write_min_config(tmp_path)
    _stub_set_at(monkeypatch, None)

    def boom() -> int:
        raise RuntimeError("keyring blew up")

    from odoo_mcp import setup_wizard

    monkeypatch.setattr(setup_wizard, "load_credentials_into_os", boom)
    monkeypatch.delenv("ODOO_MCP_DEV_USERNAME", raising=False)
    monkeypatch.delenv("ODOO_MCP_DEV_API_KEY", raising=False)
    rc = doctor.run_doctor(cfg)
    out = capsys.readouterr().out
    # Loader exception must surface as a `!` warning, not abort doctor.
    assert "!" in out
    assert "credstore" in out.lower() or "credentials" in out.lower()
    # Doctor still ran the per-instance checks (which will have failed).
    assert rc == 1


# -----------------------------------------------------------------------------
# F3 — rotation warning
# -----------------------------------------------------------------------------


def test_doctor_warns_when_api_key_older_than_threshold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _write_min_config(tmp_path, rotation_days=90)
    _stub_loader(monkeypatch)
    set_at = datetime.now(UTC) - timedelta(days=100)
    _stub_set_at(monkeypatch, set_at)
    doctor.run_doctor(cfg)
    out = capsys.readouterr().out
    assert "rotation" in out.lower()
    assert "100 days" in out
    assert "rotate-key dev" in out


def test_doctor_no_rotation_warning_when_recent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _write_min_config(tmp_path, rotation_days=90)
    _stub_loader(monkeypatch)
    set_at = datetime.now(UTC) - timedelta(days=30)
    _stub_set_at(monkeypatch, set_at)
    doctor.run_doctor(cfg)
    out = capsys.readouterr().out
    # The "API key rotation" warning row must NOT appear for fresh keys.
    assert "API key was set" not in out
    assert "Consider rotating" not in out


def test_doctor_warns_on_missing_rotation_timestamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Keys created before the v0.13.1 timestamp tracking landed have no
    set-at entry. Doctor must nudge the operator to rotate-once so the
    timestamp gets recorded going forward."""
    cfg = _write_min_config(tmp_path)
    _stub_loader(monkeypatch)
    _stub_set_at(monkeypatch, None)
    doctor.run_doctor(cfg)
    out = capsys.readouterr().out
    assert "no rotation timestamp" in out.lower()


def test_rotation_threshold_is_configurable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``rotation_warning_days = 7`` makes a 10-day-old key noisy."""
    cfg = _write_min_config(tmp_path, rotation_days=7)
    _stub_loader(monkeypatch)
    set_at = datetime.now(UTC) - timedelta(days=10)
    _stub_set_at(monkeypatch, set_at)
    doctor.run_doctor(cfg)
    out = capsys.readouterr().out
    assert "10 days" in out
    assert "threshold 7" in out


# -----------------------------------------------------------------------------
# F3 — _credstore writes timestamps for tracked secrets
# -----------------------------------------------------------------------------


def test_credstore_set_secret_records_set_at(monkeypatch: pytest.MonkeyPatch) -> None:
    from odoo_mcp import _credstore

    stored: dict[tuple[str, str], str] = {}

    def fake_set(service: str, username: str, value: str) -> None:
        stored[(service, username)] = value

    def fake_get(service: str, username: str) -> str | None:
        return stored.get((service, username))

    monkeypatch.setattr(_credstore.keyring, "set_password", fake_set)
    monkeypatch.setattr(_credstore.keyring, "get_password", fake_get)

    _credstore.set_secret("dev", "ODOO_MCP_DEV_API_KEY", "secret-key")
    # Secret stored at the canonical path.
    assert stored[("odoo-mcp/dev", "ODOO_MCP_DEV_API_KEY")] == "secret-key"
    # Sibling timestamp written under the meta path.
    ts_raw = stored.get(("odoo-mcp/dev/_meta", "ODOO_MCP_DEV_API_KEY_set_at"))
    assert ts_raw is not None
    parsed = datetime.fromisoformat(ts_raw)
    # Must be timezone-aware UTC.
    assert parsed.tzinfo is not None


def test_credstore_set_secret_does_not_track_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only ``*_API_KEY`` services get a tracking timestamp.

    Usernames don't expire; the rotation warning is per-API-key only.
    """
    from odoo_mcp import _credstore

    stored: dict[tuple[str, str], str] = {}

    def fake_set(service: str, username: str, value: str) -> None:
        stored[(service, username)] = value

    monkeypatch.setattr(_credstore.keyring, "set_password", fake_set)

    _credstore.set_secret("dev", "ODOO_MCP_DEV_USERNAME", "alice@example.com")
    assert ("odoo-mcp/dev", "ODOO_MCP_DEV_USERNAME") in stored
    # No tracking entry for the username.
    assert not any(k[0].endswith("/_meta") for k in stored)


def test_credstore_get_secret_set_at_handles_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from odoo_mcp import _credstore

    def fake_get(_service: str, _username: str) -> str | None:
        return None

    monkeypatch.setattr(_credstore.keyring, "get_password", fake_get)
    assert _credstore.get_secret_set_at("dev", "ODOO_MCP_DEV_API_KEY") is None


def test_credstore_get_secret_set_at_handles_unparseable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Corrupt timestamp string returns None rather than raising."""
    from odoo_mcp import _credstore

    def fake_get(_service: str, _username: str) -> str | None:
        return "not-a-date"

    monkeypatch.setattr(_credstore.keyring, "get_password", fake_get)
    assert _credstore.get_secret_set_at("dev", "ODOO_MCP_DEV_API_KEY") is None


# -----------------------------------------------------------------------------
# F3 — config schema accepts rotation_warning_days
# -----------------------------------------------------------------------------


def test_config_accepts_rotation_warning_days(tmp_path: Path) -> None:
    from odoo_mcp.config import load_config

    cfg = _write_min_config(tmp_path, rotation_days=42)
    loaded = load_config(cfg)
    assert loaded.defaults.rotation_warning_days == 42


def test_config_rejects_unknown_default_key(tmp_path: Path) -> None:
    from odoo_mcp.config import load_config
    from odoo_mcp.errors import ConfigError

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[defaults]\n"
        'audit_log = "/tmp/audit.jsonl"\n'
        "bogus_key = 1\n"
        "\n"
        "[instances.dev]\n"
        'url = "http://example.invalid"\n'
        'database = "db"\n'
        'credentials_env_prefix = "ODOO_MCP_DEV"\n'
        "production = false\n"
    )
    os.chmod(cfg, 0o600)
    with pytest.raises(ConfigError, match="bogus_key"):
        load_config(cfg)


# -----------------------------------------------------------------------------
# F2 — error hints are tightened (no workaround coaching)
# -----------------------------------------------------------------------------


def test_model_not_allowed_hint_no_workaround() -> None:
    from odoo_mcp.errors import ModelNotAllowedError

    err = ModelNotAllowedError("blocked")
    hint = err.hint
    assert hint is not None
    assert "administrator" in hint.lower()
    # Old hint suggested odoo_list_instances and "ask your administrator
    # to add this model to the config" — drop the second half.
    assert "odoo_list_instances" not in hint
    assert "add this model" not in hint


def test_prod_guard_hint_no_workaround() -> None:
    from odoo_mcp.errors import ProdGuardError

    err = ProdGuardError("blocked")
    hint = err.hint
    assert hint is not None
    # Old hint named the verb to call (odoo_enable_prod_writes); drop it.
    assert "odoo_enable_prod_writes" not in hint
    assert "operator" in hint.lower()


# -----------------------------------------------------------------------------
# --json output
# -----------------------------------------------------------------------------


def test_doctor_json_emits_machine_readable_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    cfg = _write_min_config(tmp_path)
    _stub_loader(monkeypatch)
    _stub_set_at(monkeypatch, None)
    monkeypatch.delenv("ODOO_MCP_DEV_USERNAME", raising=False)
    monkeypatch.delenv("ODOO_MCP_DEV_API_KEY", raising=False)
    doctor.run_doctor(cfg, as_json=True)
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert "ok" in payload
    assert "steps" in payload
    assert isinstance(payload["steps"], list)
    assert all("name" in s and "ok" in s for s in payload["steps"])


def test_doctor_main_accepts_json_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    cfg = _write_min_config(tmp_path)
    _stub_loader(monkeypatch)
    _stub_set_at(monkeypatch, None)
    monkeypatch.delenv("ODOO_MCP_DEV_USERNAME", raising=False)
    monkeypatch.delenv("ODOO_MCP_DEV_API_KEY", raising=False)
    doctor.main(["--config", str(cfg), "--json"])
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert isinstance(payload.get("ok"), bool)


def test_doctor_main_unknown_arg_returns_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = doctor.main(["--bogus"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "Unknown" in err or "Usage" in err
