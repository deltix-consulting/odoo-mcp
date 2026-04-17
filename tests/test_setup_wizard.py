"""Tests for the wizard's non-interactive subcommands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from odoo_mcp import setup_wizard

_SAMPLE_CONFIG = """\
[defaults]
timeout_seconds = 30

[instances.main]
url = "https://klantx.odoo.com"
database = "klantx-prod"
credentials_env_prefix = "ODOO_MCP_MAIN"
production = true

[instances.dev]
url = "https://dev.example.com"
database = "dev_db"
credentials_env_prefix = "ODOO_MCP_DEV"
production = false
"""


@pytest.fixture
def fake_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(_SAMPLE_CONFIG)
    cfg_path.chmod(0o600)
    monkeypatch.setattr(setup_wizard, "DEFAULT_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(setup_wizard, "_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(setup_wizard, "_LAUNCH_SH", tmp_path / "launch.sh")
    return cfg_path


@pytest.mark.usefixtures("fake_config")
def test_list_shows_instances(capsys: pytest.CaptureFixture[str]) -> None:
    rc = setup_wizard.main(["--list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Configured instances (2)" in out
    assert "main" in out
    assert "klantx.odoo.com" in out
    assert "production" in out
    assert "dev" in out


def test_list_without_config_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / "nope.toml"
    monkeypatch.setattr(setup_wizard, "DEFAULT_CONFIG_PATH", missing)
    rc = setup_wizard.main(["--list"])
    assert rc == 1


@pytest.mark.usefixtures("fake_config")
def test_regenerate_launcher_writes_file(capsys: pytest.CaptureFixture[str]) -> None:
    rc = setup_wizard.main(["--regenerate-launcher"])
    assert rc == 0
    launch_path = setup_wizard._LAUNCH_SH
    assert launch_path.exists()
    content = launch_path.read_text()
    assert "launch-env" in content
    out = capsys.readouterr().out
    assert "Regenerated" in out


@pytest.mark.usefixtures("fake_config")
def test_rotate_key_unknown_instance(capsys: pytest.CaptureFixture[str]) -> None:
    rc = setup_wizard.main(["--rotate-key", "ghost"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "ghost" in out


@pytest.mark.usefixtures("fake_config")
def test_rotate_key_updates_keychain(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[str, str, str]] = []

    def fake_set(instance: str, service: str, value: str) -> None:
        calls.append((instance, service, value))

    def fake_getpass(_prompt: str = "") -> str:
        return "new-secret-key"

    def fake_doctor() -> None:
        return None

    monkeypatch.setattr(setup_wizard, "_keychain_set", fake_set)
    monkeypatch.setattr(setup_wizard.getpass, "getpass", fake_getpass)
    monkeypatch.setattr(setup_wizard, "_run_doctor", fake_doctor)

    rc = setup_wizard.main(["--rotate-key", "dev"])
    assert rc == 0
    # Only the API_KEY entry should be updated (username left alone).
    assert len(calls) == 1
    instance, service, value = calls[0]
    assert instance == "dev"
    assert service.endswith("_API_KEY")
    assert value == "new-secret-key"
    # The new key value itself must not be echoed to stdout.
    stdout: str = capsys.readouterr().out
    assert "new-secret-key" not in stdout


@pytest.mark.usefixtures("fake_config")
def test_rotate_key_empty_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getpass(_prompt: str = "") -> str:
        return ""

    keychain_calls: list[Any] = []
    monkeypatch.setattr(setup_wizard.getpass, "getpass", fake_getpass)
    monkeypatch.setattr(
        setup_wizard,
        "_keychain_set",
        lambda *a, **_k: keychain_calls.append(a),
    )
    rc = setup_wizard.main(["--rotate-key", "dev"])
    assert rc == 1
    assert keychain_calls == []
