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
    # Regression guard for the v0.7.0 launcher template: must call the new
    # `launch` subcommand and must NOT use the old two-process `launch-env`.
    assert "python -m odoo_mcp launch" in content
    assert "launch-env" not in content
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


# ---------------------------------------------------------------------------
# Atomic config writes (v0.7.0)
# ---------------------------------------------------------------------------


def test_claude_desktop_config_write_is_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If os.replace fails after the temp file is written, the original file is untouched."""
    target = tmp_path / "claude_desktop_config.json"
    target.write_text('{"mcpServers": {"existing": {"command": "/x"}}}\n')
    monkeypatch.setattr(setup_wizard, "_CLAUDE_DESKTOP_CONFIG", target)
    monkeypatch.setattr(setup_wizard, "_LAUNCH_SH", tmp_path / "launch.sh")
    original = target.read_text()

    def boom(*_a: Any, **_k: Any) -> None:
        raise OSError("simulated EIO")

    monkeypatch.setattr(setup_wizard.os, "replace", boom)

    with pytest.raises(OSError):
        setup_wizard._register_claude_desktop()

    # Original is untouched.
    assert target.read_text() == original
    # No leftover .tmp files in the directory.
    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_claude_desktop_config_temp_file_cleaned_up_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If json serialization succeeds but write fails before replace, temp is cleaned up."""
    target = tmp_path / "claude_desktop_config.json"
    monkeypatch.setattr(setup_wizard, "_CLAUDE_DESKTOP_CONFIG", target)
    monkeypatch.setattr(setup_wizard, "_LAUNCH_SH", tmp_path / "launch.sh")

    real_chmod = setup_wizard.os.chmod

    def chmod_boom(path: Any, mode: int) -> None:
        # Only blow up on the temp-file chmod; ignore others.
        if str(path).endswith(".tmp"):
            raise OSError("simulated chmod failure")
        real_chmod(path, mode)

    monkeypatch.setattr(setup_wizard.os, "chmod", chmod_boom)

    with pytest.raises(OSError):
        setup_wizard._register_claude_desktop()

    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_write_config_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "config.toml"
    target.write_text("[defaults]\noriginal = true\n")
    monkeypatch.setattr(setup_wizard, "DEFAULT_CONFIG_PATH", target)
    monkeypatch.setattr(setup_wizard, "_CONFIG_DIR", tmp_path)
    original = target.read_text()

    def boom(*_a: Any, **_k: Any) -> None:
        raise OSError("simulated EIO")

    monkeypatch.setattr(setup_wizard.os, "replace", boom)

    with pytest.raises(OSError):
        setup_wizard._write_config({"foo": 1}, {})

    assert target.read_text() == original
    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_write_config_temp_file_cleaned_up_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "config.toml"
    monkeypatch.setattr(setup_wizard, "DEFAULT_CONFIG_PATH", target)
    monkeypatch.setattr(setup_wizard, "_CONFIG_DIR", tmp_path)

    real_chmod = setup_wizard.os.chmod

    def chmod_boom(path: Any, mode: int) -> None:
        if str(path).endswith(".tmp"):
            raise OSError("simulated chmod failure")
        real_chmod(path, mode)

    monkeypatch.setattr(setup_wizard.os, "chmod", chmod_boom)

    with pytest.raises(OSError):
        setup_wizard._write_config({"foo": 1}, {})

    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


# ---------------------------------------------------------------------------
# Uninstall (v0.7.0)
# ---------------------------------------------------------------------------


@pytest.fixture
def uninstall_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    cfg = tmp_path / "config.toml"
    cfg.write_text(_SAMPLE_CONFIG)
    cfg.chmod(0o600)
    claude_cfg = tmp_path / "claude_desktop_config.json"
    claude_cfg.write_text(
        '{"mcpServers": {"odoo-mcp": {"command": "/x"}, "other-mcp": {"command": "/y"}}}\n'
    )
    monkeypatch.setattr(setup_wizard, "DEFAULT_CONFIG_PATH", cfg)
    monkeypatch.setattr(setup_wizard, "_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(setup_wizard, "_LAUNCH_SH", tmp_path / "launch.sh")
    monkeypatch.setattr(setup_wizard, "_CLAUDE_DESKTOP_CONFIG", claude_cfg)
    (tmp_path / "launch.sh").write_text("#!/bin/bash\n")
    (tmp_path / "fields-cache.db").write_bytes(b"sqlite-stub")
    (tmp_path / "audit.jsonl").write_text("{}\n")
    (tmp_path / "audit.jsonl.2025-04-01").write_text("{}\n")
    deleted: list[tuple[str, str]] = []
    monkeypatch.setattr(
        setup_wizard,
        "_keychain_delete",
        lambda inst, svc: deleted.append((inst, svc)),
    )
    # No real `uv tool uninstall`.
    monkeypatch.setattr(
        setup_wizard.subprocess,
        "run",
        lambda *a, **k: __import__("subprocess").CompletedProcess(
            args=a, returncode=0, stdout="", stderr=""
        ),
    )
    return {
        "tmp_path": tmp_path,
        "config": cfg,
        "claude_config": claude_cfg,
        "deleted": deleted,
    }


def test_uninstall_removes_keychain_for_all_instances(
    uninstall_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("builtins.input", lambda _p="": "y")
    rc = setup_wizard._cmd_uninstall()
    assert rc == 0
    deleted: list[tuple[str, str]] = uninstall_env["deleted"]
    instances_seen = {pair[0] for pair in deleted}
    assert instances_seen == {"main", "dev"}


def test_uninstall_removes_claude_desktop_entry(
    uninstall_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import json as _json

    monkeypatch.setattr("builtins.input", lambda _p="": "y")
    setup_wizard._cmd_uninstall()
    claude_cfg: Path = uninstall_env["claude_config"]
    data = _json.loads(claude_cfg.read_text())
    assert "odoo-mcp" not in data["mcpServers"]
    assert "other-mcp" in data["mcpServers"]


def test_uninstall_aborts_on_n_confirmation(
    uninstall_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("builtins.input", lambda _p="": "n")
    rc = setup_wizard._cmd_uninstall()
    assert rc == 0
    # Nothing was deleted.
    assert uninstall_env["deleted"] == []
    assert uninstall_env["config"].exists()
    assert (uninstall_env["tmp_path"] / "launch.sh").exists()


def test_uninstall_does_not_delete_checkout(
    uninstall_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = Path(setup_wizard.__file__).resolve().parent.parent.parent
    monkeypatch.setattr("builtins.input", lambda _p="": "y")
    setup_wizard._cmd_uninstall()
    # Project dir must still exist after uninstall.
    assert project_dir.exists()


# ---------------------------------------------------------------------------
# Group check (v0.7.0)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("fake_config")
def test_group_check_warns_when_user_lacks_internal_group(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from odoo_mcp import client as client_mod

    class FakeClient:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            self.uid = 7

        def authenticate(self) -> None:
            return None

        def _execute(self, *_a: Any, **_k: Any) -> bool:
            return False

    monkeypatch.setattr(client_mod, "OdooClient", FakeClient)

    class FakeCreds:
        pass

    monkeypatch.setattr(
        "odoo_mcp.credentials.load_credentials",
        lambda _name, _prefix: FakeCreds(),
    )
    setup_wizard._check_user_is_internal("main")
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "Internal User" in out


@pytest.mark.usefixtures("fake_config")
def test_group_check_silent_when_user_has_internal_group(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from odoo_mcp import client as client_mod

    class FakeClient:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            self.uid = 7

        def authenticate(self) -> None:
            return None

        def _execute(self, *_a: Any, **_k: Any) -> bool:
            return True

    monkeypatch.setattr(client_mod, "OdooClient", FakeClient)

    class FakeCreds:
        pass

    monkeypatch.setattr(
        "odoo_mcp.credentials.load_credentials",
        lambda _name, _prefix: FakeCreds(),
    )
    setup_wizard._check_user_is_internal("main")
    out = capsys.readouterr().out
    assert "WARNING" not in out
    assert "Internal User" in out
