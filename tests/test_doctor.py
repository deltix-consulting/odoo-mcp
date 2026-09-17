"""Tests for ``odoo-mcp doctor``.

Covers the v0.13.1 fixes:

* B4 — doctor preloads credentials from the credstore so it works
  standalone, not only under ``odoo-mcp launch``.
* F3 — doctor emits a rotation-warning when an instance's API key was
  last set longer ago than ``rotation_warning_days``.
"""

from __future__ import annotations

import os
import stat
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


def test_model_not_allowed_hint_is_actionable() -> None:
    from odoo_mcp.errors import ModelNotAllowedError

    err = ModelNotAllowedError("blocked")
    hint = err.hint
    assert hint is not None
    # The hint names the exact config key and the diagnose tool — "contact
    # your administrator" dead-ends were a top source of wasted agent turns.
    assert "allowed_models" in hint
    assert "odoo_diagnose_access" in hint
    # ...but never suggests the denylist can be bypassed.
    assert "cannot be re-enabled" in hint
    assert "odoo_list_instances" not in hint


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


# -----------------------------------------------------------------------------
# File-mode rows — a chmod the writer could not apply must not stay invisible
# -----------------------------------------------------------------------------


def _write_config_with_cache(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Like ``_write_min_config`` but with the fields cache enabled.

    Returns ``(config_path, audit_log_path, fields_cache_path)``.
    """
    cfg = tmp_path / "config.toml"
    audit_log = tmp_path / "audit.jsonl"
    cache = tmp_path / "fields_cache.sqlite"
    cfg.write_text(
        "[defaults]\n"
        f'audit_log = "{audit_log}"\n'
        f'fields_cache_path = "{cache}"\n'
        "\n"
        "[instances.dev]\n"
        'url = "http://example.invalid"\n'
        'database = "db"\n'
        'credentials_env_prefix = "ODOO_MCP_DEV"\n'
        "production = false\n"
    )
    os.chmod(cfg, 0o600)
    return cfg, audit_log, cache


def _run_doctor_json(cfg: Path, capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    import json

    doctor.run_doctor(cfg, as_json=True)
    return json.loads(capsys.readouterr().out.strip())


def _warnings_named(payload: dict[str, object], name: str) -> list[str]:
    warnings = payload["warnings"]
    assert isinstance(warnings, list)
    return [w["detail"] for w in warnings if w["name"] == name]


@pytest.mark.skipif(os.name != "posix", reason="st_mode bits are POSIX-only")
def test_doctor_warns_when_audit_log_chmod_was_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``AuditLog`` swallows a refused ``chmod`` behind a logger nobody
    reads by default, and the file stays writable so "Audit log writable"
    is green. doctor must read the mode back and say so."""
    from odoo_mcp import audit

    cfg, audit_log, _cache = _write_config_with_cache(tmp_path)
    audit_log.write_text("")
    os.chmod(audit_log, 0o644)

    def refuse_chmod(path: object, mode: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(audit.os, "chmod", refuse_chmod)
    _stub_loader(monkeypatch)
    _stub_set_at(monkeypatch, None)
    monkeypatch.delenv("ODOO_MCP_DEV_USERNAME", raising=False)
    monkeypatch.delenv("ODOO_MCP_DEV_API_KEY", raising=False)

    payload = _run_doctor_json(cfg, capsys)

    steps = {s["name"]: s["ok"] for s in payload["steps"]}  # type: ignore[union-attr]
    assert steps["Audit log writable"] is True  # the writer's own check is green
    details = _warnings_named(payload, "Audit log mode")
    assert len(details) == 1
    assert str(audit_log) in details[0]
    assert "0o644" in details[0]
    assert f"chmod 600 {audit_log}" in details[0]


@pytest.mark.skipif(os.name != "posix", reason="st_mode bits are POSIX-only")
def test_doctor_no_mode_warning_when_files_are_owner_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Passes before and after the fix: a healthy install gets no row."""
    cfg, audit_log, cache = _write_config_with_cache(tmp_path)
    cache.write_bytes(b"")
    os.chmod(cache, 0o600)
    _stub_loader(monkeypatch)
    _stub_set_at(monkeypatch, None)
    monkeypatch.delenv("ODOO_MCP_DEV_USERNAME", raising=False)
    monkeypatch.delenv("ODOO_MCP_DEV_API_KEY", raising=False)

    payload = _run_doctor_json(cfg, capsys)

    # AuditLog re-hardens the current file on open, so it is 0o600 here.
    assert stat.S_IMODE(audit_log.stat().st_mode) == 0o600
    assert _warnings_named(payload, "Audit log mode") == []
    assert _warnings_named(payload, "Fields cache mode") == []


@pytest.mark.skipif(os.name != "posix", reason="st_mode bits are POSIX-only")
def test_doctor_warns_on_loose_rotated_audit_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Rotated ``audit-YYYY-MM-DD.jsonl`` files get the same best-effort
    re-harden on open as the current file. With chmod refused and the
    current file already 0o600, the rotated one is the only loose file
    and must be the one named."""
    from odoo_mcp import audit

    cfg, audit_log, _cache = _write_config_with_cache(tmp_path)
    audit_log.write_text("")
    os.chmod(audit_log, 0o600)
    rotated = tmp_path / "audit-2026-09-01.jsonl"
    rotated.write_text("")
    os.chmod(rotated, 0o640)

    def refuse_chmod(path: object, mode: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(audit.os, "chmod", refuse_chmod)
    _stub_loader(monkeypatch)
    _stub_set_at(monkeypatch, None)
    monkeypatch.delenv("ODOO_MCP_DEV_USERNAME", raising=False)
    monkeypatch.delenv("ODOO_MCP_DEV_API_KEY", raising=False)

    payload = _run_doctor_json(cfg, capsys)

    details = _warnings_named(payload, "Audit log mode")
    assert len(details) == 1
    assert str(rotated) in details[0]
    assert str(audit_log) not in details[0]


@pytest.mark.skipif(os.name != "posix", reason="st_mode bits are POSIX-only")
def test_doctor_warns_on_loose_existing_fields_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``PersistentFieldsCache`` only chmods the file it CREATES; a cache
    that already exists at 0o644 stays that way across every restart."""
    cfg, _audit_log, cache = _write_config_with_cache(tmp_path)
    cache.write_bytes(b"")
    os.chmod(cache, 0o644)
    _stub_loader(monkeypatch)
    _stub_set_at(monkeypatch, None)
    monkeypatch.delenv("ODOO_MCP_DEV_USERNAME", raising=False)
    monkeypatch.delenv("ODOO_MCP_DEV_API_KEY", raising=False)

    payload = _run_doctor_json(cfg, capsys)

    details = _warnings_named(payload, "Fields cache mode")
    assert len(details) == 1
    assert str(cache) in details[0]
    assert f"chmod 600 {cache}" in details[0]


@pytest.mark.skipif(os.name != "posix", reason="st_mode bits are POSIX-only")
def test_doctor_mode_rows_are_warnings_not_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A loose mode matches the writers' best-effort posture: yellow, and
    it must not flip the exit code on its own."""
    cfg, _audit_log, cache = _write_config_with_cache(tmp_path)
    cache.write_bytes(b"")
    os.chmod(cache, 0o644)
    _stub_loader(monkeypatch)
    _stub_set_at(monkeypatch, None)
    monkeypatch.delenv("ODOO_MCP_DEV_USERNAME", raising=False)
    monkeypatch.delenv("ODOO_MCP_DEV_API_KEY", raising=False)

    payload = _run_doctor_json(cfg, capsys)

    step_names = {s["name"] for s in payload["steps"]}  # type: ignore[union-attr]
    assert "Fields cache mode" not in step_names
    assert _warnings_named(payload, "Fields cache mode")


@pytest.mark.skipif(os.name != "posix", reason="st_mode bits are POSIX-only")
def test_doctor_mode_check_runs_before_the_instance_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The per-instance loop ``continue``s on missing credentials. A
    config-only check must not sit behind that gate — the tests above
    all run with credentials removed, so this pins the ordering that
    makes them meaningful."""
    cfg, _audit_log, cache = _write_config_with_cache(tmp_path)
    cache.write_bytes(b"")
    os.chmod(cache, 0o644)
    _stub_loader(monkeypatch)
    _stub_set_at(monkeypatch, None)
    monkeypatch.delenv("ODOO_MCP_DEV_USERNAME", raising=False)
    monkeypatch.delenv("ODOO_MCP_DEV_API_KEY", raising=False)

    payload = _run_doctor_json(cfg, capsys)

    steps = {s["name"]: s["ok"] for s in payload["steps"]}  # type: ignore[union-attr]
    assert steps["[dev] credentials"] is False
    assert _warnings_named(payload, "Fields cache mode")
