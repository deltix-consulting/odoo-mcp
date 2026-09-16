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
# Strict-mode allowed_models smoke test — every entry, deterministic order
# -----------------------------------------------------------------------------
#
# ``InstanceConfig.allowed_models`` is a ``frozenset[str]``. Doctor used to
# probe ``next(iter(...))`` — one arbitrary member, and a different one on
# every run because ``str`` hashing is salted per process. A strict list with
# one typo'd model therefore made doctor red on the run that happened to draw
# the typo and green otherwise. These tests drive the per-instance branch with
# a fake client (every earlier test in this file stops at the credentials
# step, so the smoke test had never been executed by the suite).


def _write_strict_config(tmp_path: Path, allowed_models: list[str]) -> Path:
    cfg = tmp_path / "config.toml"
    audit_log = tmp_path / "audit.jsonl"
    models = ", ".join(f'"{m}"' for m in allowed_models)
    cfg.write_text(
        "[defaults]\n"
        f'audit_log = "{audit_log}"\n'
        'fields_cache_path = ""\n'
        "\n"
        "[instances.dev]\n"
        'url = "http://example.invalid"\n'
        'database = "db"\n'
        'credentials_env_prefix = "ODOO_MCP_DEV"\n'
        "production = false\n"
        f"allowed_models = [{models}]\n"
    )
    os.chmod(cfg, 0o600)
    return cfg


class _FakeDoctorClient:
    """Stands in for ``OdooClient`` inside ``run_doctor``.

    ``rejects`` is the set of model names whose ``fields_get`` raises the
    fault Odoo returns for an unknown model; ``transport_error`` makes every
    probe raise a transport failure instead. ``probed`` records call order.
    """

    probed: list[str] = []
    rejects: frozenset[str] = frozenset()
    transport_error: bool = False
    uid = 7
    is_admin = False
    admin_reason = ""

    def __init__(self, _inst: object, _creds: object) -> None:
        pass

    def authenticate(self) -> None:
        pass

    def fields_get(self, model: str) -> dict[str, dict[str, str]]:
        from odoo_mcp.errors import OdooRemoteError, OdooTransportError

        type(self).probed.append(model)
        if self.transport_error:
            raise OdooTransportError(f"Timeout calling {model}.fields_get on 'dev' after 30s")
        if model in self.rejects:
            raise OdooRemoteError(
                f"Odoo fault on {model}.fields_get: Traceback (most recent call last):\n"
                f'  File "/odoo/odoo/api.py", line 1, in call_kw\n'
                f"KeyError: '{model}'",
                server_text=True,
            )
        return {"id": {"type": "integer"}, "name": {"type": "char"}}


def _drive_doctor(
    monkeypatch: pytest.MonkeyPatch,
    cfg: Path,
    *,
    rejects: frozenset[str] = frozenset(),
    transport_error: bool = False,
    as_json: bool = False,
) -> tuple[int, list[str]]:
    """Run doctor to completion against the fake client; return (rc, probed)."""
    _stub_loader(monkeypatch)
    _stub_set_at(monkeypatch, datetime.now(UTC))
    monkeypatch.setattr(doctor, "_print_update_check", lambda: None)
    monkeypatch.setenv("ODOO_MCP_DEV_USERNAME", "bot")
    monkeypatch.setenv("ODOO_MCP_DEV_API_KEY", "not-a-real-key")
    monkeypatch.setattr(_FakeDoctorClient, "probed", [])
    monkeypatch.setattr(_FakeDoctorClient, "rejects", rejects)
    monkeypatch.setattr(_FakeDoctorClient, "transport_error", transport_error)
    monkeypatch.setattr(doctor, "OdooClient", _FakeDoctorClient)
    rc = doctor.run_doctor(cfg, as_json=as_json)
    return rc, list(_FakeDoctorClient.probed)


def test_doctor_strict_mode_probes_every_allowed_model_in_sorted_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    models = ["sale.order", "res.partner", "crm.lead", "account.move"]
    cfg = _write_strict_config(tmp_path, models)
    rc, probed = _drive_doctor(monkeypatch, cfg)
    out = capsys.readouterr().out
    assert rc == 0
    # Every listed model, once each, in a run-independent order.
    assert probed == sorted(models)
    assert "✓ [dev] fields_get(allowed_models) — 4 models verified, 8 fields" in out


def test_doctor_strict_mode_reports_a_typo_wherever_it_sits_in_the_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    models = ["res.partner", "sale.order", "crm.lead", "account.move", "crm.laed"]
    cfg = _write_strict_config(tmp_path, models)
    rc, probed = _drive_doctor(monkeypatch, cfg, rejects=frozenset({"crm.laed"}))
    out = capsys.readouterr().out
    assert rc == 1
    assert "crm.laed" in probed
    # Stable step name (not fields_get(<whichever model was drawn>)), the
    # offending entry named, and the actionable half: which TOML to fix.
    assert "✗ [dev] fields_get(allowed_models)" in out
    assert "1 of 5 listed model(s) rejected by Odoo: crm.laed" in out
    assert "fix allowed_models in [instances.dev]" in out
    # Only the trailing line of Odoo's traceback, never the whole thing.
    assert "Odoo said: KeyError: 'crm.laed'" in out
    assert "Traceback" not in out
    # The models that ARE fine were still probed — the check does not stop
    # at the first rejection, so an operator sees every typo in one run.
    assert set(probed) == set(models)


def test_doctor_strict_mode_lists_every_rejected_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _write_strict_config(tmp_path, ["res.partner", "sale.ordr", "crm.laed"])
    rc, _ = _drive_doctor(monkeypatch, cfg, rejects=frozenset({"sale.ordr", "crm.laed"}))
    out = capsys.readouterr().out
    assert rc == 1
    assert "2 of 3 listed model(s) rejected by Odoo: crm.laed, sale.ordr" in out


def test_doctor_strict_mode_warns_on_entries_the_mcp_itself_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # ``ir.config_parameter`` is on MODEL_DENYLIST: listing it grants nothing
    # because check_model refuses it before the allowlist is consulted.
    # ``res partner`` fails the name-shape check the same way.
    cfg = _write_strict_config(tmp_path, ["res.partner", "ir.config_parameter", "res partner"])
    rc, probed = _drive_doctor(monkeypatch, cfg)
    out = capsys.readouterr().out
    # Inert entries are a hygiene warning, not a failure: the instance still
    # serves res.partner.
    assert rc == 0
    assert "✓ [dev] fields_get(allowed_models) — 1 models verified" in out
    assert "! [dev] allowed_models — 2 of 3 listed model(s) can never be reached" in out
    assert "ir.config_parameter, res partner" in out
    assert "Remove them from [instances.dev] allowed_models" in out
    # Inert entries are never sent to Odoo: a fields_get on a denylisted
    # model would succeed there and make "verified" a lie.
    assert probed == ["res.partner"]


def test_doctor_strict_mode_with_only_inert_entries_is_red(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _write_strict_config(tmp_path, ["ir.config_parameter", "ir.rule"])
    rc, probed = _drive_doctor(monkeypatch, cfg)
    out = capsys.readouterr().out
    assert rc == 1
    assert probed == []
    assert "✗ [dev] fields_get(allowed_models) — no reachable model" in out


def test_doctor_strict_mode_transport_error_fails_fast(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _write_strict_config(tmp_path, ["res.partner", "sale.order", "crm.lead"])
    rc, probed = _drive_doctor(monkeypatch, cfg, transport_error=True)
    out = capsys.readouterr().out
    assert rc == 1
    # A timeout tells us nothing model-specific; the remaining probes would
    # fail the same way, so the step fails on the first one with its message.
    assert probed == ["crm.lead"]
    assert "✗ [dev] fields_get(allowed_models) — crm.lead: Timeout calling" in out


def test_doctor_open_mode_still_probes_res_partner_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Guards the fix against over-reaching: open mode has no hand-typed list
    # to verify, so the pre-existing single probe and its step name stay.
    cfg = _write_strict_config(tmp_path, ["*"])
    rc, probed = _drive_doctor(monkeypatch, cfg)
    out = capsys.readouterr().out
    assert rc == 0
    assert probed == ["res.partner"]
    assert "✓ [dev] fields_get(res.partner) — 2 fields" in out


def test_doctor_json_step_name_is_stable_in_strict_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    # ``--json`` is documented "for CI / dashboards"; a step whose name
    # depended on which model was drawn could not be tracked across runs.
    cfg = _write_strict_config(tmp_path, ["sale.order", "res.partner", "crm.laed"])
    rc, _ = _drive_doctor(monkeypatch, cfg, rejects=frozenset({"crm.laed"}), as_json=True)
    payload = json.loads(capsys.readouterr().out.strip())
    assert rc == 1
    names = [s["name"] for s in payload["steps"]]
    assert "[dev] fields_get(allowed_models)" in names
    step = next(s for s in payload["steps"] if s["name"] == "[dev] fields_get(allowed_models)")
    assert step["ok"] is False
    assert "crm.laed" in step["detail"]
