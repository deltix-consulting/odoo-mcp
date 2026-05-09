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
