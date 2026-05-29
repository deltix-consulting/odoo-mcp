"""Tests for the wizard's non-interactive subcommands."""

from __future__ import annotations

import os
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
    """Legacy fallback: ``setup --regenerate-launcher`` still writes a
    runnable ``launch.sh`` for users who haven't migrated yet (v0.13.0
    keeps the template alive for one release as a fallback)."""
    if os.name != "posix":
        pytest.skip("launch.sh fallback is posix-only")
    rc = setup_wizard.main(["--regenerate-launcher"])
    assert rc == 0
    launch_path = setup_wizard._LAUNCH_SH
    assert launch_path.exists()
    content = launch_path.read_text()
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


def test_register_codex_preserves_existing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "config.toml"
    target.write_text(
        'personality = "pragmatic"\n\n[plugins."github@openai-curated"]\nenabled = true\n'
    )
    monkeypatch.setattr(setup_wizard, "_CODEX_CONFIG", target)
    monkeypatch.setattr(setup_wizard, "_resolve_odoo_mcp_command", lambda: "/bin/odoo-mcp")

    assert setup_wizard._register_codex() is True

    content = target.read_text()
    assert 'personality = "pragmatic"' in content
    assert '[plugins."github@openai-curated"]' in content
    assert "[mcp_servers.odoo-mcp]" in content
    assert 'command = "/bin/odoo-mcp"' in content
    assert 'args = ["launch"]' in content


def test_register_codex_replaces_existing_odoo_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "config.toml"
    target.write_text(
        "[mcp_servers.odoo-mcp]\n"
        'command = "/old"\n'
        'args = ["old"]\n\n'
        "[projects.foo]\n"
        'trust_level = "trusted"\n'
    )
    monkeypatch.setattr(setup_wizard, "_CODEX_CONFIG", target)
    monkeypatch.setattr(setup_wizard, "_resolve_odoo_mcp_command", lambda: "/new/odoo-mcp")

    assert setup_wizard._register_codex() is True

    content = target.read_text()
    assert 'command = "/old"' not in content
    assert 'args = ["old"]' not in content
    assert 'command = "/new/odoo-mcp"' in content
    assert "[projects.foo]" in content


def test_register_codex_skips_when_codex_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "missing" / "config.toml"
    monkeypatch.setattr(setup_wizard, "_CODEX_CONFIG", target)
    monkeypatch.setattr(setup_wizard.shutil, "which", lambda _name: None)

    assert setup_wizard._register_codex() is False
    assert not target.exists()


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
    codex_cfg = tmp_path / "codex_config.toml"
    codex_cfg.write_text(
        'personality = "pragmatic"\n\n'
        "[mcp_servers.odoo-mcp]\n"
        'command = "/x"\n'
        'args = ["launch"]\n\n'
        "[mcp_servers.other-mcp]\n"
        'command = "/y"\n'
    )
    monkeypatch.setattr(setup_wizard, "_CODEX_CONFIG", codex_cfg)
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
        "codex_config": codex_cfg,
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


def test_uninstall_removes_codex_entry(
    uninstall_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("builtins.input", lambda _p="": "y")
    setup_wizard._cmd_uninstall()
    codex_cfg: Path = uninstall_env["codex_config"]
    content = codex_cfg.read_text()
    assert "[mcp_servers.odoo-mcp]" not in content
    assert "[mcp_servers.other-mcp]" in content


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


def test_collect_launch_env_refuses_loose_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_collect_launch_env` must refuse a 0o644 config before any Keychain access.

    The launch path runs before ``build_app``'s permissions check, so it
    needs its own gate to keep credentials from being injected into
    ``os.environ`` against a loose config.
    """
    import os

    if os.name != "posix":
        pytest.skip("permissions check is posix-only")

    from odoo_mcp import setup_wizard
    from odoo_mcp.errors import ConfigError

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[instances.x]\nurl = "https://x"\ndatabase = "d"\n'
        'credentials_env_prefix = "ODOO_MCP_X"\nproduction = false\n'
    )
    cfg.chmod(0o644)  # loose

    monkeypatch.setattr(setup_wizard, "DEFAULT_CONFIG_PATH", cfg)

    # If the gate fails to fire, the test would proceed to call Keychain;
    # blow up if that happens so we can tell the gate didn't gate.
    def boom(_name: str, _service: str) -> str:
        raise AssertionError("Keychain accessed despite loose-perm config")

    monkeypatch.setattr(setup_wizard, "_keychain_get", boom)

    with pytest.raises(ConfigError, match="loose permissions"):
        setup_wizard._collect_launch_env()


def test_claude_desktop_config_path_per_platform(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`_claude_desktop_config_path` returns the right OS-specific path."""
    import platform as _platform

    # macOS
    monkeypatch.setattr(_platform, "system", lambda: "Darwin")
    p = setup_wizard._claude_desktop_config_path()
    assert "Library/Application Support/Claude/claude_desktop_config.json" in p.as_posix()

    # Windows
    monkeypatch.setattr(_platform, "system", lambda: "Windows")
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    p = setup_wizard._claude_desktop_config_path()
    assert p.name == "claude_desktop_config.json"
    assert "Claude" in p.parts
    assert "Roaming" in p.parts

    # Linux
    monkeypatch.setattr(_platform, "system", lambda: "Linux")
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    p = setup_wizard._claude_desktop_config_path()
    assert ".config/Claude/claude_desktop_config.json" in p.as_posix()


def test_codex_config_path_uses_codex_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    assert setup_wizard._codex_config_path() == tmp_path / "codex-home" / "config.toml"


def test_keychain_get_migrates_legacy_macos_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from odoo_mcp import _credstore

    stored: list[tuple[str, str, str]] = []

    monkeypatch.setattr(_credstore, "get_secret", lambda _inst, _svc: None)
    monkeypatch.setattr(
        _credstore, "set_secret", lambda inst, svc, val: stored.append((inst, svc, val))
    )
    monkeypatch.setattr(setup_wizard.platform, "system", lambda: "Darwin")

    def fake_run(*_args: Any, **_kwargs: Any) -> Any:
        return __import__("subprocess").CompletedProcess(
            args=[],
            returncode=0,
            stdout="legacy-secret\n",
            stderr="",
        )

    monkeypatch.setattr(setup_wizard.subprocess, "run", fake_run)

    assert setup_wizard._keychain_get("prod", "ODOO_MCP_PROD_API_KEY") == "legacy-secret"
    assert stored == [("prod", "ODOO_MCP_PROD_API_KEY", "legacy-secret")]


def test_credstore_set_get_delete_via_wizard_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wizard's ``_keychain_*`` names are credstore-backed in v0.13.0."""
    from odoo_mcp import _credstore

    store: dict[tuple[str, str], str] = {}

    def fake_set(inst: str, svc: str, val: str) -> None:
        store[(inst, svc)] = val

    def fake_get(inst: str, svc: str) -> str | None:
        return store.get((inst, svc))

    def fake_del(inst: str, svc: str) -> None:
        store.pop((inst, svc), None)

    monkeypatch.setattr(_credstore, "set_secret", fake_set)
    monkeypatch.setattr(_credstore, "get_secret", fake_get)
    monkeypatch.setattr(_credstore, "delete_secret", fake_del)

    setup_wizard._keychain_set("main", "API_KEY", "v")
    assert setup_wizard._keychain_get("main", "API_KEY") == "v"
    setup_wizard._keychain_delete("main", "API_KEY")
    assert setup_wizard._keychain_get("main", "API_KEY") is None


# ---------------------------------------------------------------------------
# Admin acknowledgment flow (setup wizard catches admin-on-prod before doctor)
# ---------------------------------------------------------------------------


_PROD_ADMIN_CONFIG = """\
[defaults]
timeout_seconds = 30

[instances.prod]
url = "https://klantx.odoo.com"
database = "klantx-prod"
credentials_env_prefix = "ODOO_MCP_PROD"
production = true
"""


def _wire_admin_fakes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    raise_admin_refusal: bool,
) -> Path:
    """Common scaffolding: pretend a config exists and stub out the auth call."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(_PROD_ADMIN_CONFIG)
    cfg_path.chmod(0o600)
    monkeypatch.setattr(setup_wizard, "DEFAULT_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(setup_wizard, "_CONFIG_DIR", tmp_path)

    from odoo_mcp import client as client_module
    from odoo_mcp.credentials import Credentials
    from odoo_mcp.errors import OdooAuthError

    def fake_load_credentials(name: str, prefix: str) -> Credentials:
        return Credentials(instance_name=name, username="admin@example.com", _api_key="k" * 10)

    monkeypatch.setattr("odoo_mcp.credentials.load_credentials", fake_load_credentials)

    def fake_authenticate(self: Any) -> int:
        if raise_admin_refusal:
            raise OdooAuthError(
                "Refusing to use admin credentials (system administrator "
                "(base.group_system)) on production instance 'prod'. "
                "Admin keys bypass per-user Odoo record rules..."
            )
        self._uid = 7  # noqa: SLF001
        return 7

    monkeypatch.setattr(client_module.OdooClient, "authenticate", fake_authenticate)
    return cfg_path


def test_acknowledge_admin_writes_opt_out_when_user_acknowledges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Wizard prompts and persists the opt-out when the user types 'acknowledge'."""
    cfg_path = _wire_admin_fakes(tmp_path, monkeypatch, raise_admin_refusal=True)

    answers = iter(["acknowledge"])
    monkeypatch.setattr(setup_wizard, "_ask", lambda *_a, **_kw: next(answers))

    proceeded = setup_wizard._acknowledge_admin_or_abort("prod")
    assert proceeded is True

    # Opt-out is now persisted in the toml.
    import tomllib

    body = tomllib.loads(cfg_path.read_text())
    assert body["instances"]["prod"]["refuse_admin_on_production"] is False

    out = capsys.readouterr().out
    assert "Admin-credentials detected" in out
    assert "ACKNOWLEDGE THE RISK" in out


def test_acknowledge_admin_aborts_when_user_declines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Anything other than 'acknowledge' aborts and leaves the toml untouched."""
    cfg_path = _wire_admin_fakes(tmp_path, monkeypatch, raise_admin_refusal=True)

    answers = iter(["no"])
    monkeypatch.setattr(setup_wizard, "_ask", lambda *_a, **_kw: next(answers))

    proceeded = setup_wizard._acknowledge_admin_or_abort("prod")
    assert proceeded is False

    # No opt-out written.
    import tomllib

    body = tomllib.loads(cfg_path.read_text())
    assert "refuse_admin_on_production" not in body["instances"]["prod"]

    out = capsys.readouterr().out
    assert "Aborted" in out


def test_acknowledge_admin_skips_for_non_admin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-admin authenticate returns True without prompting."""
    _wire_admin_fakes(tmp_path, monkeypatch, raise_admin_refusal=False)

    # _ask must not be called — wire it to fail loudly if it is.
    def fail_ask(*_a: Any, **_kw: Any) -> str:
        raise AssertionError("must not prompt for non-admin auth")

    monkeypatch.setattr(setup_wizard, "_ask", fail_ask)

    assert setup_wizard._acknowledge_admin_or_abort("prod") is True


def test_acknowledge_admin_skips_when_already_opted_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the toml already has refuse_admin_on_production=false we don't prompt."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        _PROD_ADMIN_CONFIG.replace(
            "production = true",
            "production = true\nrefuse_admin_on_production = false",
        )
    )
    cfg_path.chmod(0o600)
    monkeypatch.setattr(setup_wizard, "DEFAULT_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(setup_wizard, "_CONFIG_DIR", tmp_path)

    def fail_ask(*_a: Any, **_kw: Any) -> str:
        raise AssertionError("must not prompt when already acknowledged")

    monkeypatch.setattr(setup_wizard, "_ask", fail_ask)
    assert setup_wizard._acknowledge_admin_or_abort("prod") is True


def test_acknowledge_admin_skips_for_non_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Admin on a dev instance is fine — the gate is prod-only."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(_PROD_ADMIN_CONFIG.replace("production = true", "production = false"))
    cfg_path.chmod(0o600)
    monkeypatch.setattr(setup_wizard, "DEFAULT_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(setup_wizard, "_CONFIG_DIR", tmp_path)

    def fail_ask(*_a: Any, **_kw: Any) -> str:
        raise AssertionError("must not prompt on non-production instance")

    monkeypatch.setattr(setup_wizard, "_ask", fail_ask)
    assert setup_wizard._acknowledge_admin_or_abort("prod") is True


def test_acknowledge_admin_cli_persists_opt_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`odoo-mcp setup --acknowledge-admin NAME` writes the opt-out and exits 0."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(_PROD_ADMIN_CONFIG)
    cfg_path.chmod(0o600)
    monkeypatch.setattr(setup_wizard, "DEFAULT_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(setup_wizard, "_CONFIG_DIR", tmp_path)
    answers = iter(["acknowledge"])
    monkeypatch.setattr(setup_wizard, "_ask", lambda *_a, **_kw: next(answers))

    rc = setup_wizard.main(["--acknowledge-admin", "prod"])
    assert rc == 0

    import tomllib

    body = tomllib.loads(cfg_path.read_text())
    assert body["instances"]["prod"]["refuse_admin_on_production"] is False
    out = capsys.readouterr().out
    assert "refuse_admin_on_production = false written" in out


def test_acknowledge_admin_cli_aborts_when_user_declines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(_PROD_ADMIN_CONFIG)
    cfg_path.chmod(0o600)
    monkeypatch.setattr(setup_wizard, "DEFAULT_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(setup_wizard, "_CONFIG_DIR", tmp_path)
    answers = iter(["no"])
    monkeypatch.setattr(setup_wizard, "_ask", lambda *_a, **_kw: next(answers))

    rc = setup_wizard.main(["--acknowledge-admin", "prod"])
    assert rc == 1

    import tomllib

    body = tomllib.loads(cfg_path.read_text())
    assert "refuse_admin_on_production" not in body["instances"]["prod"]


def test_acknowledge_admin_cli_unknown_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(_PROD_ADMIN_CONFIG)
    cfg_path.chmod(0o600)
    monkeypatch.setattr(setup_wizard, "DEFAULT_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(setup_wizard, "_CONFIG_DIR", tmp_path)

    rc = setup_wizard.main(["--acknowledge-admin", "ghost"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "ghost" in out


def test_acknowledge_admin_cli_missing_arg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(_PROD_ADMIN_CONFIG)
    cfg_path.chmod(0o600)
    monkeypatch.setattr(setup_wizard, "DEFAULT_CONFIG_PATH", cfg_path)

    rc = setup_wizard.main(["--acknowledge-admin", ""])
    assert rc == 2
    out = capsys.readouterr().out
    assert "Usage" in out


def test_toml_value_escapes_carriage_return() -> None:
    """`_toml_value` must escape \\r alongside \\n / \\t."""
    import tomllib

    from odoo_mcp.setup_wizard import _toml_value

    raw = "line1\r\nline2\rtail"
    serialized = _toml_value(raw)
    # Round-trip through tomllib to ensure the escape produces a valid
    # TOML string and reads back to the original value.
    parsed = tomllib.loads(f"v = {serialized}\n")
    assert parsed["v"] == raw


# ---------------------------------------------------------------------------
# _ask_api_key — choice between paste and generate-via-password
# ---------------------------------------------------------------------------


def test_ask_api_key_paste_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Choice 1 returns the pasted key, never touches XML-RPC."""
    monkeypatch.setattr(setup_wizard, "_ask", lambda *_a, **_kw: "1")
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "pasted-key-value")
    key = setup_wizard._ask_api_key("https://x.odoo.com", "db", "u@x.com", "prod")
    assert key == "pasted-key-value"


def test_ask_api_key_generate_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Choice 2 generates the key via password and returns it."""
    monkeypatch.setattr(setup_wizard, "_ask", lambda *_a, **_kw: "2")
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "the-password")
    monkeypatch.setattr(
        setup_wizard,
        "_generate_api_key_via_password",
        lambda url, db, user, pw, name: ("generated-key", 0),
    )
    key = setup_wizard._ask_api_key("https://x.odoo.com", "db", "u@x.com", "prod")
    assert key == "generated-key"


def test_ask_api_key_generate_falls_back_to_manual(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """If generation fails (e.g. 2FA), the wizard falls back to manual paste."""
    monkeypatch.setattr(setup_wizard, "_ask", lambda *_a, **_kw: "2")
    # First getpass = password for generation, second = manual key fallback.
    answers = iter(["the-password", "manual-fallback-key"])
    monkeypatch.setattr("getpass.getpass", lambda _prompt: next(answers))

    def _boom(*_a: object, **_kw: object) -> str:
        raise setup_wizard._KeyGenError("2FA enabled — password auth blocked")

    monkeypatch.setattr(setup_wizard, "_generate_api_key_via_password", _boom)
    key = setup_wizard._ask_api_key("https://x.odoo.com", "db", "u@x.com", "prod")
    assert key == "manual-fallback-key"
    out = capsys.readouterr().out
    assert "Falling back to manual entry" in out


def _install_fake_opener(monkeypatch: pytest.MonkeyPatch, *responses: Any) -> list[Any]:
    """Local mirror of the helper in test_renew_key — fake out
    ``urllib.request.build_opener`` so opener.open returns the queued
    JSON-RPC responses in order. See test_renew_key for full notes.
    """
    import json
    import urllib.request
    from unittest.mock import MagicMock

    queue = list(responses)
    captured: list[Any] = []

    class _FakeResponse:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *_):  # type: ignore[no-untyped-def]
            return False

        def read(self) -> bytes:
            return self._body

    def fake_open(req: Any, timeout: float | None = None) -> _FakeResponse:
        captured.append(req)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeResponse(json.dumps(item).encode("utf-8"))

    opener = MagicMock()
    opener.open.side_effect = fake_open
    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **_kw: opener)
    return captured


def test_generate_api_key_via_password_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Authenticate, cleanup search (no stale), description create, make_key.
    _install_fake_opener(
        monkeypatch,
        {"jsonrpc": "2.0", "result": {"uid": 5}},
        {"jsonrpc": "2.0", "result": []},
        {"jsonrpc": "2.0", "result": 7},
        {
            "jsonrpc": "2.0",
            "result": {
                "type": "ir.actions.act_window",
                "context": {"default_key": "fresh-generated-key"},
            },
        },
    )
    key, num_cleaned = setup_wizard._generate_api_key_via_password(
        "https://x.odoo.com", "db", "u@x.com", "pw", "odoo-mcp (prod)"
    )
    assert key == "fresh-generated-key"
    assert num_cleaned == 0


def test_generate_api_key_via_password_unlinks_stale_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Search returns stale ids → unlink call fires → num_cleaned reflects it."""
    import json

    captured = _install_fake_opener(
        monkeypatch,
        {"jsonrpc": "2.0", "result": {"uid": 5}},
        {"jsonrpc": "2.0", "result": [101, 102]},
        {"jsonrpc": "2.0", "result": True},  # unlink
        {"jsonrpc": "2.0", "result": 7},
        {
            "jsonrpc": "2.0",
            "result": {"type": "ir.actions.act_window", "context": {"default_key": "fresh-key"}},
        },
    )
    key, num_cleaned = setup_wizard._generate_api_key_via_password(
        "https://x.odoo.com", "db", "u@x.com", "pw", "odoo-mcp (prod) on host"
    )
    assert key == "fresh-key"
    assert num_cleaned == 2
    # Third call (index 2) is the unlink — verify it targets the right ids.
    unlink_body = json.loads(captured[2].data.decode("utf-8"))["params"]
    assert unlink_body["model"] == "res.users.apikeys"
    assert unlink_body["method"] == "unlink"
    assert unlink_body["args"] == [[101, 102]]


def test_mcp_key_name_includes_hostname() -> None:
    name = setup_wizard._mcp_key_name("prod")
    assert name.startswith("odoo-mcp (prod) on ")
    # Whatever the host name is, the suffix is non-empty.
    assert name.split(" on ", 1)[1].strip()


# ---------------------------------------------------------------------------
# `make_key` wizard plumbing — these are the actual reason we exist now that
# the legacy `_generate` path is gone. Each test pins one shape so a
# refactor can't quietly regress to "Private methods cannot be called
# remotely" theatre.
# ---------------------------------------------------------------------------


def test_format_keygen_fault_private_method_points_at_update() -> None:
    """The old-client diagnostic: if Odoo says "private methods", tell the
    user to upgrade rather than dump the raw error."""
    msg = setup_wizard._format_keygen_fault(
        "Private methods (such as 'res.users.apikeys._generate') cannot be called remotely."
    )
    assert "Upgrade" in msg or "upgrade" in msg
    assert "odoo-mcp update" in msg


def test_format_keygen_fault_check_identity_explains_rerun() -> None:
    """A stale identity stamp on Odoo ≥17 should hint that re-running
    works (the timestamp gets refreshed within ~10 minutes of
    authenticate) and fall back to manual creation if it still fails."""
    msg = setup_wizard._format_keygen_fault(
        "Please re-enter your password to confirm your identity."
    )
    assert "identity" in msg.lower() or "re-enter" in msg.lower()
    assert "rerun" in msg.lower() or "Account Security" in msg


def test_format_keygen_fault_http_only_gives_actionable_message() -> None:
    """The exact NL error Timon hit on Odoo Online before the web-JSON-RPC
    rewrite: ``Deze methode is alleen toegankelijk via HTTP``. Should
    point at upgrading or manual creation — not just dump the raw text."""
    msg = setup_wizard._format_keygen_fault("Deze methode is alleen toegankelijk via HTTP")
    assert "HTTP session" in msg or "HTTP" in msg
    assert "Account Security" in msg or "odoo-mcp update" in msg
    assert "option 1" in msg or "manually" in msg.lower()


def test_format_keygen_fault_http_only_english_variant() -> None:
    """English wording of the same error path — pin it explicitly so a
    locale change in Odoo doesn't slip past the detector."""
    msg = setup_wizard._format_keygen_fault("This method can only be accessed over HTTP")
    assert "HTTP" in msg
    assert "Account Security" in msg or "odoo-mcp update" in msg


def test_format_keygen_fault_generic_points_at_manual_path() -> None:
    """Any other Odoo fault still gets the manual-creation instructions —
    the user always has a way forward, even when we don't recognise the error."""
    msg = setup_wizard._format_keygen_fault("Something went sideways in Odoo.")
    assert "Something went sideways" in msg
    assert "Account Security" in msg
    assert "option 1" in msg


def test_extract_key_from_make_key_action_default_key() -> None:
    """The canonical Odoo 17+ shape: act_window with context.default_key."""
    action = {
        "type": "ir.actions.act_window",
        "context": {"default_key": "abcdef0123456789"},
    }
    assert setup_wizard._extract_key_from_make_key_result(action) == "abcdef0123456789"


def test_extract_key_from_make_key_action_default_key_value() -> None:
    """Some Odoo forks use ``default_key_value``; accept that variant too."""
    action = {"context": {"default_key_value": "forked-key"}}
    assert setup_wizard._extract_key_from_make_key_result(action) == "forked-key"


def test_extract_key_from_make_key_raw_string() -> None:
    """Very old Odoo versions returned the raw string. Accept it."""
    assert setup_wizard._extract_key_from_make_key_result("legacy-raw-key") == "legacy-raw-key"


def test_extract_key_from_make_key_top_level_key() -> None:
    """Some Odoo forks return the key at the top level, not nested."""
    assert (
        setup_wizard._extract_key_from_make_key_result({"key": "top-level-key"}) == "top-level-key"
    )


def test_extract_key_from_make_key_params_key() -> None:
    """Observed on a couple of Odoo Online tenants: key inside ``params``."""
    action = {"type": "ir.actions.client", "params": {"key": "in-params-key"}}
    assert setup_wizard._extract_key_from_make_key_result(action) == "in-params-key"


def test_is_identity_check_redirect_by_res_model() -> None:
    """The canonical identity-check action carries the dotted model name."""
    assert setup_wizard._is_identity_check_redirect(
        {"type": "ir.actions.act_window", "res_model": "res.users.identitycheck"}
    )


def test_is_identity_check_redirect_by_action_name() -> None:
    """Fallback: some Odoo skins drop the res_model but keep an identity name."""
    assert setup_wizard._is_identity_check_redirect(
        {"name": "Confirm your identity", "views": [(False, "form")]}
    )


def test_is_identity_check_redirect_negative() -> None:
    """The normal API-key-ready action must NOT match the identity heuristic."""
    assert not setup_wizard._is_identity_check_redirect(
        {"res_model": "res.users.apikeys.show", "context": {"default_key": "k"}}
    )
    assert not setup_wizard._is_identity_check_redirect(None)
    assert not setup_wizard._is_identity_check_redirect("string")


def test_describe_action_shape_leaks_no_values() -> None:
    """Diagnostic summary must surface enough structure for bug reports
    without ever including the field VALUES (which could contain the
    very API key we're trying to extract)."""
    secret = "ZZZ-leaked-key-value-must-not-appear-anywhere-ZZZ"
    summary = setup_wizard._describe_action_shape(
        {
            "type": "ir.actions.act_window",
            "res_model": "res.users.apikeys.show",
            "context": {"default_key": secret, "lang": "en_US"},
        }
    )
    assert secret not in summary
    assert "en_US" not in summary  # context values stay private too
    # But the keys and the safe identifier fields (type, res_model) are present
    # so the maintainer can diagnose from the bug report.
    assert "default_key" in summary
    assert "res_model" in summary
    assert "res.users.apikeys.show" in summary


def test_generate_api_key_via_password_identity_check_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: when make_key returns the identity-check wizard, the
    caller gets a *specific* error explaining what happened — not the
    generic "unrecognised shape" message."""
    _install_fake_opener(
        monkeypatch,
        {"jsonrpc": "2.0", "result": {"uid": 5}},
        {"jsonrpc": "2.0", "result": []},
        {"jsonrpc": "2.0", "result": 9},
        {
            "jsonrpc": "2.0",
            "result": {
                "type": "ir.actions.act_window",
                "res_model": "res.users.identitycheck",
                "name": "Confirm your identity",
            },
        },
    )
    with pytest.raises(setup_wizard._KeyGenError, match="identity-check wizard"):
        setup_wizard._generate_api_key_via_password(
            "https://x.odoo.com", "db", "u@x.com", "pw", "name"
        )


def test_extract_key_from_make_key_unrecognised_shape_returns_none() -> None:
    """Anything we don't recognise → None so the caller raises the
    "create manually" error instead of writing rubbish to the keychain."""
    assert setup_wizard._extract_key_from_make_key_result({"foo": "bar"}) is None
    assert setup_wizard._extract_key_from_make_key_result(None) is None
    assert setup_wizard._extract_key_from_make_key_result(42) is None
    assert setup_wizard._extract_key_from_make_key_result({"context": {}}) is None


def test_generate_api_key_via_password_uses_make_key_not_underscore_generate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hard guarantee: the flow never calls a method that starts with an
    underscore. Pinning this catches a future refactor that "helpfully"
    reintroduces the direct ``_generate`` call — which Odoo blocks
    unconditionally, the failure that motivated this whole rewrite."""
    import json

    captured = _install_fake_opener(
        monkeypatch,
        {"jsonrpc": "2.0", "result": {"uid": 5}},
        {"jsonrpc": "2.0", "result": []},
        {"jsonrpc": "2.0", "result": 9},
        {"jsonrpc": "2.0", "result": {"context": {"default_key": "k"}}},
    )

    setup_wizard._generate_api_key_via_password("https://x.odoo.com", "db", "u@x.com", "pw", "name")

    # Inspect every captured call_kw payload (skip the authenticate one,
    # which has no ``method`` field — its endpoint is the auth route).
    for req in captured:
        if not req.full_url.endswith("/web/dataset/call_kw"):
            continue
        method = json.loads(req.data.decode("utf-8"))["params"]["method"]
        assert not method.startswith("_"), (
            f"Refused to send underscore-prefixed method {method!r} over RPC — "
            f"Odoo blocks these unconditionally."
        )


def test_generate_api_key_via_password_unrecognised_make_key_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If make_key returns something we can't decode, raise — never silently
    pass a non-key value through to the caller (who would write it to the
    OS keychain as if it were a real key)."""
    _install_fake_opener(
        monkeypatch,
        {"jsonrpc": "2.0", "result": {"uid": 5}},
        {"jsonrpc": "2.0", "result": []},
        {"jsonrpc": "2.0", "result": 9},
        {"jsonrpc": "2.0", "result": {"unexpected": "shape"}},
    )
    with pytest.raises(setup_wizard._KeyGenError, match="expected shape"):
        setup_wizard._generate_api_key_via_password(
            "https://x.odoo.com", "db", "u@x.com", "pw", "name"
        )


def test_generate_api_key_via_password_wrong_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auth-step JSON-RPC errors become a 'rejected the password' message
    with a 2FA hint — this is the most common interactive failure."""
    _install_fake_opener(
        monkeypatch,
        {"jsonrpc": "2.0", "error": {"data": {"message": "Access denied"}}},
    )
    with pytest.raises(setup_wizard._KeyGenError, match="rejected the password"):
        setup_wizard._generate_api_key_via_password(
            "https://x.odoo.com", "db", "u@x.com", "wrong", "odoo-mcp (prod)"
        )


def test_generate_api_key_via_password_network_error_is_friendly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connection failures become a single-line readable message; no Python
    traceback leaks into the user's terminal."""
    _install_fake_opener(monkeypatch, OSError("Connection refused"))
    with pytest.raises(setup_wizard._KeyGenError, match="Could not reach Odoo"):
        setup_wizard._generate_api_key_via_password(
            "https://x.odoo.com", "db", "u@x.com", "pw", "name"
        )


# ---------------------------------------------------------------------------
# `odoo-mcp setup --scheduler-config` — emit the snippet a non-Claude
# scheduler (n8n, Decisions, custom cron) needs to load odoo-mcp.
#
# Triggering scenario: a scheduled cron job ran with no MCP loaded, the
# spawned agent reported "Odoo MCP niet geladen", no invoices got
# checked. We can't auto-write into an external scheduler's config but
# we can print the exact snippet to paste, with the host-specific
# absolute path resolved.
# ---------------------------------------------------------------------------


def test_scheduler_config_json_emits_mcp_servers_shape(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(setup_wizard, "_resolve_odoo_mcp_command", lambda: "/abs/path/odoo-mcp")
    rc = setup_wizard.main(["--scheduler-config"])
    assert rc == 0
    captured = capsys.readouterr()
    # The snippet itself is on stdout so it can be redirected straight
    # into a file; instructions go to stderr.
    import json as _json

    payload = _json.loads(captured.out)
    assert payload == {
        "mcpServers": {
            "odoo-mcp": {
                "command": "/abs/path/odoo-mcp",
                "args": ["launch"],
            }
        }
    }
    # Instructions on stderr point at verification.
    assert "tool list" in captured.err or "tools" in captured.err


def test_scheduler_config_env_format_emits_keyvalue_lines(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(setup_wizard, "_resolve_odoo_mcp_command", lambda: "/abs/path/odoo-mcp")
    rc = setup_wizard.main(["--scheduler-config", "--format=env"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ODOO_MCP_COMMAND=/abs/path/odoo-mcp" in out
    assert "ODOO_MCP_ARGS=launch" in out


def test_scheduler_config_cli_format_emits_bare_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(setup_wizard, "_resolve_odoo_mcp_command", lambda: "/abs/path/odoo-mcp")
    rc = setup_wizard.main(["--scheduler-config", "--format=cli"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == "/abs/path/odoo-mcp launch"


def test_scheduler_config_rejects_unknown_format(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mistyped formats must exit non-zero — silently falling back to
    JSON would hide the typo and the operator would paste an unexpected
    shape into their scheduler config."""
    monkeypatch.setattr(setup_wizard, "_resolve_odoo_mcp_command", lambda: "/abs/path/odoo-mcp")
    rc = setup_wizard.main(["--scheduler-config", "--format=yaml"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "yaml" in err.lower()
    assert "json" in err.lower()  # Suggests the valid options


def test_scheduler_config_format_with_space_separator(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--format env`` (space-separated) must work the same as
    ``--format=env``; this is the default ``_extract_flag_value``
    behaviour and the test pins it so a refactor can't quietly
    break script-friendly invocations."""
    monkeypatch.setattr(setup_wizard, "_resolve_odoo_mcp_command", lambda: "/abs/path/odoo-mcp")
    rc = setup_wizard.main(["--scheduler-config", "--format", "env"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ODOO_MCP_COMMAND=" in out
