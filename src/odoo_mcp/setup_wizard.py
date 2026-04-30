"""Interactive CLI wizard for first-time setup, adding, and removing Odoo instances.

Invoked via::

    odoo-mcp setup           # first-time guided setup
    odoo-mcp setup --add     # add an instance to existing config
    odoo-mcp setup --remove  # remove an instance

All prompts use stdlib ``input()`` / ``getpass.getpass()``. Credentials are
stored in the macOS Keychain via ``security(1)``. No external dependencies.
"""

from __future__ import annotations

import contextlib
import getpass
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from .config import _DEFAULT_ALLOWED_MODELS, DEFAULT_CONFIG_PATH

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CONFIG_DIR: Path = DEFAULT_CONFIG_PATH.parent
_LAUNCH_SH: Path = _CONFIG_DIR / "launch.sh"
_CLAUDE_DESKTOP_CONFIG: Path = Path(
    "~/Library/Application Support/Claude/claude_desktop_config.json"
).expanduser()
_KEYCHAIN_ACCOUNT_PREFIX = "odoo-mcp-"


# ---------------------------------------------------------------------------
# Atomic file write helper
# ---------------------------------------------------------------------------


def _atomic_write_text(target: Path, content: str, *, mode: int = 0o600) -> None:
    """Write *content* to *target* atomically with chmod *mode*.

    Writes to a temp file in the same directory (so ``os.replace`` is atomic
    on the same filesystem), chmods it, then replaces the target. On any
    exception during write/chmod the temp file is cleaned up. If
    ``os.replace`` itself raises (rare), the temp file is also cleaned up
    and the original target is left untouched — this is the property we care
    most about: a half-written config never replaces a working one.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115 — manual close: needed to chmod + replace
        mode="w",
        dir=str(target.parent),
        delete=False,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp.name)
    try:
        try:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        finally:
            tmp.close()
        if os.name == "posix":
            os.chmod(tmp_path, mode)
        os.replace(tmp_path, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError, OSError):
            tmp_path.unlink()
        raise


# ---------------------------------------------------------------------------
# TOML serialisation (stdlib tomllib is read-only)
# ---------------------------------------------------------------------------


def _toml_value(value: object) -> str:
    """Serialise a single Python value to its TOML representation."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\t", "\\t")
        )
        return f'"{escaped}"'
    if isinstance(value, (list, tuple)):
        inner = ", ".join(_toml_value(v) for v in value)
        return f"[{inner}]"
    raise ValueError(f"Unsupported TOML type: {type(value)}")


def _generate_toml(defaults: dict[str, Any], instances: dict[str, dict[str, Any]]) -> str:
    """Build a complete config.toml string from *defaults* and *instances*."""
    lines: list[str] = ["[defaults]"]
    for key, val in defaults.items():
        lines.append(f"{key} = {_toml_value(val)}")
    lines.append("")

    for name, inst in instances.items():
        lines.append(f"[instances.{name}]")
        for key, val in inst.items():
            lines.append(f"{key} = {_toml_value(val)}")
        lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# TOML round-trip helpers (read existing config into dicts)
# ---------------------------------------------------------------------------


def _load_raw_config() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Read existing config.toml and return (defaults_dict, instances_dict)."""
    with DEFAULT_CONFIG_PATH.open("rb") as f:
        raw = tomllib.load(f)
    defaults: dict[str, Any] = dict(raw.get("defaults", {}))
    instances: dict[str, dict[str, Any]] = {}
    for name, entry in raw.get("instances", {}).items():
        instances[name] = dict(entry)
    return defaults, instances


# ---------------------------------------------------------------------------
# Keychain helpers
# ---------------------------------------------------------------------------


def _keychain_set(instance_name: str, service: str, value: str) -> None:
    """Store a value in the macOS Keychain (create or update)."""
    account = f"{_KEYCHAIN_ACCOUNT_PREFIX}{instance_name}"
    subprocess.run(  # noqa: S603, S607 — intentional call to macOS security(1)
        [
            "/usr/bin/security",
            "add-generic-password",
            "-U",
            "-a",
            account,
            "-s",
            service,
            "-w",
            value,
        ],
        check=True,
        capture_output=True,
    )


def _keychain_delete(instance_name: str, service: str) -> None:
    """Delete a Keychain entry. Silently ignores 'not found' errors."""
    account = f"{_KEYCHAIN_ACCOUNT_PREFIX}{instance_name}"
    subprocess.run(  # noqa: S603, S607 — intentional call to macOS security(1)
        [
            "/usr/bin/security",
            "delete-generic-password",
            "-a",
            account,
            "-s",
            service,
        ],
        capture_output=True,
    )


def _keychain_get(instance_name: str, service: str) -> str | None:
    """Read a value from the macOS Keychain. Returns None on failure."""
    account = f"{_KEYCHAIN_ACCOUNT_PREFIX}{instance_name}"
    result = subprocess.run(  # noqa: S603, S607 — intentional call to macOS security(1)
        [
            "/usr/bin/security",
            "find-generic-password",
            "-a",
            account,
            "-s",
            service,
            "-w",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# launch-env helper (called by launch.sh at runtime)
# ---------------------------------------------------------------------------


def _collect_launch_env() -> tuple[dict[str, str], list[str]]:
    """Resolve all (USERNAME, API_KEY) pairs from Keychain.

    Returns ``(env_vars, errors)``. ``env_vars`` is a flat dict suitable for
    splatting into ``os.environ.update``; ``errors`` is a list of human-
    readable strings describing missing Keychain entries.

    Raises ``FileNotFoundError`` if the config file is missing — callers
    decide how to surface that.
    """
    if not DEFAULT_CONFIG_PATH.exists():
        raise FileNotFoundError(DEFAULT_CONFIG_PATH)

    with DEFAULT_CONFIG_PATH.open("rb") as f:
        raw = tomllib.load(f)

    instances: dict[str, Any] = raw.get("instances", {})
    env: dict[str, str] = {}
    errors: list[str] = []

    for name, entry in instances.items():
        if not isinstance(entry, dict):
            continue
        prefix = entry.get("credentials_env_prefix", "")
        if not prefix:
            continue

        username_service = f"{prefix}_USERNAME"
        api_key_service = f"{prefix}_API_KEY"

        username = _keychain_get(name, username_service)
        api_key = _keychain_get(name, api_key_service)

        if username is None:
            errors.append(f"Keychain entry not found: {username_service} for instance {name}")
            continue
        if api_key is None:
            errors.append(f"Keychain entry not found: {api_key_service} for instance {name}")
            continue

        env[username_service] = username
        env[api_key_service] = api_key

    return env, errors


def print_launch_env() -> int:
    """Print ``export VAR=val`` lines for all configured instances.

    Kept for backward compat with old launchers that still ``eval`` this
    output. New launchers use ``python -m odoo_mcp launch`` which loads
    Keychain credentials directly into ``os.environ``.
    """
    try:
        env, errors = _collect_launch_env()
    except FileNotFoundError:
        print("Config not found. Run: odoo-mcp setup", file=sys.stderr)
        return 1

    for var, value in env.items():
        # Shell-escape values by using single quotes with embedded quote escaping
        safe = value.replace("'", "'\\''")
        print(f"export {var}='{safe}'")

    for err in errors:
        print(f"# WARNING: {err}", file=sys.stderr)

    return 1 if errors and not env else 0


def load_launch_env_into_os() -> int:
    """Resolve Keychain credentials and set them in ``os.environ``.

    Returns 0 on success, 1 if config is missing, 2 if any Keychain entry
    is missing. Warnings are written to stderr but do not abort — the
    server itself will surface a clearer error when the affected instance
    is touched.
    """
    try:
        env, errors = _collect_launch_env()
    except FileNotFoundError:
        print("Config not found. Run: odoo-mcp setup", file=sys.stderr)
        return 1

    for var, value in env.items():
        os.environ[var] = value
    for err in errors:
        print(f"WARNING: {err}", file=sys.stderr)

    if errors and not env:
        return 2
    return 0


# ---------------------------------------------------------------------------
# Interactive prompts
# ---------------------------------------------------------------------------

_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")


def _ask(prompt: str, default: str = "", validator: str = "") -> str:
    """Prompt the user. Returns stripped input or *default*."""
    suffix = f" [{default}]" if default else ""
    while True:
        raw = input(f"{prompt}{suffix}: ").strip()
        value = raw or default
        if not value:
            print("  A value is required.")
            continue
        if validator == "name" and not _NAME_RE.match(value):
            print("  Must start with a letter and contain only letters, digits, underscores.")
            continue
        if validator == "url" and not (value.startswith("https://") or value.startswith("http://")):
            print("  Must start with https:// or http://")
            continue
        return value


def _ask_bool(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{prompt} [{hint}]: ").strip().lower()
        if raw == "":
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("  Please enter y or n.")


def _ask_instance() -> dict[str, str | bool]:
    """Interactively collect instance details. Returns a dict."""
    name = _ask("Instance name", default="main", validator="name")
    url = _ask("Odoo URL (https://... or http://...)", validator="url")
    database = _ask("Database name")
    production = _ask_bool("Is this a production instance?", default=True)
    username = _ask("Username (email)")
    api_key = getpass.getpass("API key (will not echo): ")
    if not api_key.strip():
        print("API key cannot be empty.")
        sys.exit(1)
    return {
        "name": name,
        "url": url,
        "database": database,
        "production": production,
        "username": username,
        "api_key": api_key.strip(),
    }


# ---------------------------------------------------------------------------
# File generation
# ---------------------------------------------------------------------------


def _env_prefix(name: str) -> str:
    return f"ODOO_MCP_{name.upper()}"


def _write_config(defaults: dict[str, Any], instances: dict[str, dict[str, Any]]) -> None:
    """Write config.toml with chmod 600 atomically."""
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    content = _generate_toml(defaults, instances)
    _atomic_write_text(DEFAULT_CONFIG_PATH, content, mode=0o600)


def _write_launch_sh() -> None:
    """Generate launch.sh.

    Since v0.7.0 we use the unified ``python -m odoo_mcp launch`` subcommand
    which loads credentials from Keychain and starts the server in one Python
    process. Previously this was two ``uv run`` invocations (~150-300ms each
    of interpreter startup). The old ``launch-env`` subcommand still exists
    for backward compatibility with old launchers.
    """
    project_dir = Path(__file__).resolve().parent.parent.parent
    script = f"""\
#!/bin/bash
set -euo pipefail
exec uv run --directory '{project_dir}' python -m odoo_mcp launch "$@"
"""
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _LAUNCH_SH.write_text(script)
    if os.name == "posix":
        _LAUNCH_SH.chmod(0o700)


def _store_credentials(instance_name: str, prefix: str, username: str, api_key: str) -> None:
    """Store username + API key in macOS Keychain."""
    _keychain_set(instance_name, f"{prefix}_USERNAME", username)
    _keychain_set(instance_name, f"{prefix}_API_KEY", api_key)
    print(f"  Stored credentials in Keychain (account: {_KEYCHAIN_ACCOUNT_PREFIX}{instance_name})")


def _delete_credentials(instance_name: str, prefix: str) -> None:
    """Remove username + API key from macOS Keychain."""
    _keychain_delete(instance_name, f"{prefix}_USERNAME")
    _keychain_delete(instance_name, f"{prefix}_API_KEY")
    print(f"  Removed credentials from Keychain for {instance_name}")


def _register_claude_desktop() -> None:
    """Add odoo-mcp to Claude Desktop config (atomic write)."""
    config: dict[str, Any] = {}
    if _CLAUDE_DESKTOP_CONFIG.exists():
        try:
            config = json.loads(_CLAUDE_DESKTOP_CONFIG.read_text())
        except (json.JSONDecodeError, OSError):
            print(f"  Warning: could not read {_CLAUDE_DESKTOP_CONFIG}, creating new config.")

    if "mcpServers" not in config:
        config["mcpServers"] = {}

    config["mcpServers"]["odoo-mcp"] = {
        "command": str(_LAUNCH_SH),
        "args": [],
    }

    _atomic_write_text(
        _CLAUDE_DESKTOP_CONFIG,
        json.dumps(config, indent=2) + "\n",
        mode=0o600,
    )
    print(f"  Registered in Claude Desktop config: {_CLAUDE_DESKTOP_CONFIG}")


def _run_doctor() -> None:
    """Run doctor checks inline."""
    print("\nRunning doctor checks...")
    from .doctor import run_doctor

    run_doctor()


def _check_user_is_internal(name: str) -> None:
    """Verify the just-created instance's API key has ``base.group_user``.

    Portal / external users authenticate fine but cannot access most
    business models — many MCP tools will appear broken. Surface this
    early with a clear message rather than letting the user discover it
    via cryptic ACL failures later. Best-effort: any exception is
    swallowed so a transient issue doesn't fail the whole wizard.
    """
    try:
        from .client import OdooClient
        from .config import load_config
        from .credentials import load_credentials

        cfg = load_config(DEFAULT_CONFIG_PATH)
        instance = cfg.instances.get(name)
        if instance is None:
            return
        creds = load_credentials(instance.name, instance.credentials_env_prefix)
        client = OdooClient(instance, credentials=creds)
        client.authenticate()
        is_user = client._execute("res.users", "has_group", [client.uid, "base.group_user"], {})
        if bool(is_user):
            print("  ✓ API key user has the Internal User group.")
        else:
            print(
                "  ! WARNING: this API key does not have the basic Internal User "
                "group (base.group_user). Many tools may not work. Ask your Odoo "
                "admin to give the user the appropriate groups for their role."
            )
    except Exception as exc:  # noqa: BLE001 — best-effort post-flight check
        logger.debug("internal-user check skipped: %s", exc)


# ---------------------------------------------------------------------------
# Subcommand: setup (first-time)
# ---------------------------------------------------------------------------


def _default_defaults() -> dict[str, Any]:
    """The baseline [defaults] block written into a freshly created config.toml."""
    return {
        "timeout_seconds": 30,
        "max_records_default": 50,
        "max_records_hard_cap": 500,
        "allowed_models": list(_DEFAULT_ALLOWED_MODELS),
    }


def _instance_block(info: dict[str, str | bool], prefix: str) -> dict[str, Any]:
    """Shape the prompt answers into the config.toml [instances.NAME] dict."""
    return {
        "url": str(info["url"]),
        "database": str(info["database"]),
        "credentials_env_prefix": prefix,
        "production": bool(info["production"]),
    }


def _print_setup_summary(name: str, url: str) -> None:
    print("\n--- Setup complete ---")
    print(f"  Config:       {DEFAULT_CONFIG_PATH}")
    print(f"  Launcher:     {_LAUNCH_SH}")
    print(f"  Instance:     {name} ({url})")
    print("\nNext steps:")
    print("  1. Restart Claude Desktop to pick up the new MCP.")
    print("  2. Run 'odoo-mcp doctor' any time to verify connectivity.")
    print("  3. Use 'odoo-mcp setup --add' to add more instances.")


def _cmd_setup() -> int:
    """First-time setup wizard."""
    if DEFAULT_CONFIG_PATH.exists():
        print(f"Config already exists at {DEFAULT_CONFIG_PATH}")
        print("  Use 'odoo-mcp setup --add' to add an instance.")
        print("  Use 'odoo-mcp setup --remove' to remove one.")
        return 0

    print("Welcome to the Odoo MCP setup wizard.\n")
    info = _ask_instance()
    name = str(info["name"])
    prefix = _env_prefix(name)

    print("\nStoring credentials in macOS Keychain...")
    _store_credentials(name, prefix, str(info["username"]), str(info["api_key"]))

    print("\nGenerating config.toml...")
    _write_config(_default_defaults(), {name: _instance_block(info, prefix)})
    print(f"  Written to {DEFAULT_CONFIG_PATH} (chmod 600)")

    print("\nGenerating launch.sh...")
    _write_launch_sh()
    print(f"  Written to {_LAUNCH_SH} (chmod 700)")

    print("\nRegistering in Claude Desktop...")
    _register_claude_desktop()

    _run_doctor()
    _check_user_is_internal(name)
    _print_setup_summary(name, str(info["url"]))
    return 0


# ---------------------------------------------------------------------------
# Subcommand: setup --add
# ---------------------------------------------------------------------------


def _cmd_add() -> int:
    """Add a new instance to existing config."""
    if not DEFAULT_CONFIG_PATH.exists():
        print(f"No config found at {DEFAULT_CONFIG_PATH}")
        print("Run 'odoo-mcp setup' first for initial setup.")
        return 1

    defaults, instances = _load_raw_config()
    print("Add a new Odoo instance.\n")
    info = _ask_instance()
    name = str(info["name"])
    if name in instances:
        print(f"\nInstance '{name}' already exists in config. Choose a different name.")
        return 1
    prefix = _env_prefix(name)

    print("\nStoring credentials in macOS Keychain...")
    _store_credentials(name, prefix, str(info["username"]), str(info["api_key"]))

    instances[name] = _instance_block(info, prefix)
    _write_config(defaults, instances)
    print(f"  Updated {DEFAULT_CONFIG_PATH}")

    _run_doctor()
    _check_user_is_internal(name)
    print(f"\nInstance '{name}' added successfully.")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: setup --remove
# ---------------------------------------------------------------------------


def _pick_instance_to_remove(instances: dict[str, dict[str, Any]]) -> str | None:
    """Show configured instances and let the user pick one by index or name."""
    print("Configured instances:")
    names = list(instances.keys())
    for i, name in enumerate(names, 1):
        print(f"  {i}. {name} ({instances[name].get('url', '?')})")
    choice = _ask(f"\nWhich instance to remove? [1-{len(names)}]")
    try:
        idx = int(choice) - 1
        if idx < 0 or idx >= len(names):
            raise ValueError("out of range")
        return names[idx]
    except ValueError:
        if choice in names:
            return choice
        print("Invalid choice.")
        return None


def _cmd_remove() -> int:
    """Remove an instance from config."""
    if not DEFAULT_CONFIG_PATH.exists():
        print(f"No config found at {DEFAULT_CONFIG_PATH}")
        print("Run 'odoo-mcp setup' first.")
        return 1

    defaults, instances = _load_raw_config()
    if not instances:
        print("No instances configured.")
        return 0

    target = _pick_instance_to_remove(instances)
    if target is None:
        return 1
    prefix = instances[target].get("credentials_env_prefix", _env_prefix(target))
    if not _ask_bool(f"Remove instance '{target}'?", default=False):
        print("Cancelled.")
        return 0

    print("\nRemoving credentials from Keychain...")
    _delete_credentials(target, str(prefix))
    del instances[target]
    _write_config(defaults, instances)
    print(f"  Updated {DEFAULT_CONFIG_PATH}")
    if not instances:
        print("\n  Warning: no instances remain. The MCP server won't start without at least one.")

    _run_doctor()
    print(f"\nInstance '{target}' removed.")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: setup --list
# ---------------------------------------------------------------------------


def _print_instances_table(instances: dict[str, dict[str, Any]]) -> None:
    name_w = max((len(n) for n in instances), default=4)
    url_w = max((len(str(i.get("url", ""))) for i in instances.values()), default=3)
    db_w = max((len(str(i.get("database", ""))) for i in instances.values()), default=8)
    for name, entry in instances.items():
        url = str(entry.get("url", ""))
        db = str(entry.get("database", ""))
        env = "production" if bool(entry.get("production", False)) else "dev"
        print(f"  {name.ljust(name_w)}  {url.ljust(url_w)}  {db.ljust(db_w)}  ({env})")


def _cmd_list() -> int:
    """Print configured instances without making any changes."""
    if not DEFAULT_CONFIG_PATH.exists():
        print(f"No config found at {DEFAULT_CONFIG_PATH}")
        return 1
    _, instances = _load_raw_config()
    if not instances:
        print("No instances configured.")
        return 0
    print(f"Configured instances ({len(instances)}):")
    _print_instances_table(instances)
    return 0


# ---------------------------------------------------------------------------
# Subcommand: setup --rotate-key NAME
# ---------------------------------------------------------------------------


def _cmd_rotate_key(name: str) -> int:
    """Rotate the API key for one instance in the Keychain."""
    if not DEFAULT_CONFIG_PATH.exists():
        print(f"No config found at {DEFAULT_CONFIG_PATH}")
        return 1
    _, instances = _load_raw_config()
    if name not in instances:
        print(f"Instance {name!r} is not configured.")
        print(f"Known instances: {sorted(instances.keys())}")
        return 1
    prefix = str(instances[name].get("credentials_env_prefix") or _env_prefix(name))

    print(f"Rotating API key for instance '{name}' (prefix {prefix}).")
    new_key = getpass.getpass("New API key (will not echo): ").strip()
    if not new_key:
        print("API key cannot be empty. Aborted.")
        return 1

    _keychain_set(name, f"{prefix}_API_KEY", new_key)
    print(f"  Updated API key in Keychain for instance '{name}'.")

    print("\nVerifying new key with doctor...")
    try:
        _run_doctor()
    except Exception as exc:  # noqa: BLE001 — best effort verification
        print(f"  Warning: doctor run raised {type(exc).__name__}: {exc}")
        print("  The new key may be wrong. Re-run 'odoo-mcp doctor' after debugging.")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: setup --regenerate-launcher
# ---------------------------------------------------------------------------


def _cmd_regenerate_launcher() -> int:
    """Overwrite ~/.odoo-mcp/launch.sh with the current template."""
    _write_launch_sh()
    print(f"Regenerated {_LAUNCH_SH}")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: uninstall
# ---------------------------------------------------------------------------


def _remove_odoo_mcp_from_claude_desktop() -> bool:
    """Strip the ``odoo-mcp`` entry from Claude Desktop config (atomic).

    Returns True if an entry was removed. Other ``mcpServers`` entries are
    preserved untouched.
    """
    if not _CLAUDE_DESKTOP_CONFIG.exists():
        return False
    try:
        config = json.loads(_CLAUDE_DESKTOP_CONFIG.read_text())
    except (json.JSONDecodeError, OSError):
        print(f"  Warning: could not read {_CLAUDE_DESKTOP_CONFIG}; leaving it alone.")
        return False
    servers = config.get("mcpServers")
    if not isinstance(servers, dict) or "odoo-mcp" not in servers:
        return False
    del servers["odoo-mcp"]
    _atomic_write_text(
        _CLAUDE_DESKTOP_CONFIG,
        json.dumps(config, indent=2) + "\n",
        mode=0o600,
    )
    return True


def _remove_audit_logs() -> list[Path]:
    """Delete the active audit log + any rotated audit-log files."""
    removed: list[Path] = []
    if not _CONFIG_DIR.exists():
        return removed
    for path in sorted(_CONFIG_DIR.iterdir()):
        if path.name == "audit.jsonl" or path.name.startswith("audit.jsonl."):
            with contextlib.suppress(FileNotFoundError, OSError):
                path.unlink()
                removed.append(path)
    return removed


def _cmd_uninstall() -> int:
    """Remove every trace of odoo-mcp from this machine.

    Best-effort: a failure in one step does not abort the others. The
    project checkout (~/odoo-mcp by default) is intentionally left alone —
    we don't risk eating uncommitted local work.
    """
    project_dir = Path(__file__).resolve().parent.parent.parent

    print("This will remove:")
    print(f"  - All Keychain entries under '{_KEYCHAIN_ACCOUNT_PREFIX}*'")
    print(f"  - {_CONFIG_DIR} (config.toml, launch.sh, audit logs, fields cache)")
    print(f"  - The 'odoo-mcp' entry in {_CLAUDE_DESKTOP_CONFIG}")
    print("  - The 'odoo-mcp' uv tool installation")
    print()
    print(f"It will NOT remove the project checkout at {project_dir}.")
    print(f"  -> rm -rf '{project_dir}' yourself if desired.")
    print()
    if not _ask_bool("Proceed with uninstall?", default=False):
        print("Aborted.")
        return 0

    instances: dict[str, dict[str, Any]] = {}
    if DEFAULT_CONFIG_PATH.exists():
        try:
            _, instances = _load_raw_config()
        except Exception as exc:  # noqa: BLE001 — diagnostic only
            print(f"  Warning: could not read config: {exc}")

    print()
    print("Removing Keychain entries...")
    for name, entry in instances.items():
        prefix = str(entry.get("credentials_env_prefix") or _env_prefix(name))
        _delete_credentials(name, prefix)

    print("\nRemoving Claude Desktop registration...")
    if _remove_odoo_mcp_from_claude_desktop():
        print(f"  Removed odoo-mcp entry from {_CLAUDE_DESKTOP_CONFIG}")
    else:
        print("  No odoo-mcp entry found (already gone).")

    print("\nRemoving local files...")
    files_to_remove = [
        _LAUNCH_SH,
        DEFAULT_CONFIG_PATH,
        _CONFIG_DIR / "fields-cache.db",
    ]
    for path in files_to_remove:
        if path.exists():
            with contextlib.suppress(FileNotFoundError, OSError):
                path.unlink()
                print(f"  Removed {path}")
    audit_removed = _remove_audit_logs()
    for path in audit_removed:
        print(f"  Removed {path}")

    print("\nUninstalling 'odoo-mcp' from uv tool...")
    try:
        result = subprocess.run(  # noqa: S603, S607
            ["uv", "tool", "uninstall", "odoo-mcp"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            print("  Removed via 'uv tool uninstall odoo-mcp'.")
        else:
            print(f"  Warning: 'uv tool uninstall' failed: {result.stderr.strip()}")
    except FileNotFoundError:
        print("  Warning: 'uv' not on PATH; skip 'uv tool uninstall odoo-mcp'.")

    print()
    print("--- Uninstall complete ---")
    print(f"To finish: rm -rf '{project_dir}'  (your project checkout)")
    print("Restart Claude Desktop to drop the now-stale MCP entry.")
    return 0


def uninstall_main(argv: list[str] | None = None) -> int:
    """Entry point used by ``__main__.py`` for ``odoo-mcp uninstall``."""
    args = list(argv if argv is not None else [])
    if args and args[0] in {"-h", "--help"}:
        print("Usage: odoo-mcp uninstall")
        return 0
    try:
        return _cmd_uninstall()
    except KeyboardInterrupt:
        print("\n\nUninstall cancelled.")
        return 130


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _extract_flag_value(args: list[str], flag: str) -> str | None:
    """Return the value for ``--flag VALUE`` or ``--flag=VALUE``; None if absent."""
    for i, a in enumerate(args):
        if a == flag:
            if i + 1 >= len(args):
                return ""
            return args[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def main(argv: list[str] | None = None) -> int:
    """Dispatch setup subcommands."""
    args = list(argv if argv is not None else sys.argv[1:])
    try:
        if "--uninstall" in args:
            return _cmd_uninstall()
        if "--list" in args:
            return _cmd_list()
        if "--regenerate-launcher" in args:
            return _cmd_regenerate_launcher()
        rotate = _extract_flag_value(args, "--rotate-key")
        if rotate is not None:
            if not rotate:
                print("Usage: odoo-mcp setup --rotate-key NAME")
                return 2
            return _cmd_rotate_key(rotate)
        if "--add" in args:
            return _cmd_add()
        if "--remove" in args:
            return _cmd_remove()
        return _cmd_setup()
    except KeyboardInterrupt:
        print("\n\nSetup cancelled.")
        return 130
