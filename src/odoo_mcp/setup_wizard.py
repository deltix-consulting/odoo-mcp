"""Interactive CLI wizard for first-time setup, adding, and removing Odoo instances.

Invoked via::

    odoo-mcp setup           # first-time guided setup
    odoo-mcp setup --add     # add an instance to existing config
    odoo-mcp setup --remove  # remove an instance

All prompts use stdlib ``input()`` / ``getpass.getpass()``. Credentials are
stored in the OS credential store (macOS Keychain / Windows Credential Manager
/ Linux libsecret) via the cross-platform ``keyring`` package.
"""

from __future__ import annotations

import contextlib
import getpass
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from . import _credstore
from .config import _DEFAULT_ALLOWED_MODELS, DEFAULT_CONFIG_PATH, _check_file_permissions

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def _claude_desktop_config_path() -> Path:
    """Return the platform-specific path to ``claude_desktop_config.json``.

    - macOS: ``~/Library/Application Support/Claude/claude_desktop_config.json``
    - Windows: ``%APPDATA%\\Claude\\claude_desktop_config.json``
      (= ``~/AppData/Roaming/Claude/claude_desktop_config.json``)
    - Linux / other: ``~/.config/Claude/claude_desktop_config.json`` (XDG)
    """
    system = platform.system()
    if system == "Darwin":
        return Path("~/Library/Application Support/Claude/claude_desktop_config.json").expanduser()
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path("~/AppData/Roaming").expanduser()
        return base / "Claude" / "claude_desktop_config.json"
    # Linux + everything else: XDG
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path("~/.config").expanduser()
    return base / "Claude" / "claude_desktop_config.json"


def _codex_config_path() -> Path:
    """Return Codex's user config path.

    Codex uses ``$CODEX_HOME/config.toml`` when set, otherwise
    ``~/.codex/config.toml``. This path is intentionally simpler than
    Claude's platform-specific JSON path because Codex keeps the same
    home-directory convention across platforms.
    """
    home = os.environ.get("CODEX_HOME")
    base = Path(home).expanduser() if home else Path("~/.codex").expanduser()
    return base / "config.toml"


_CONFIG_DIR: Path = DEFAULT_CONFIG_PATH.parent
_LAUNCH_SH: Path = _CONFIG_DIR / "launch.sh"
_CLAUDE_DESKTOP_CONFIG: Path = _claude_desktop_config_path()
_CODEX_CONFIG: Path = _codex_config_path()
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
            .replace("\r", "\\r")
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
# Credential-store helpers
# ---------------------------------------------------------------------------
#
# These are thin wrappers around :mod:`odoo_mcp._credstore` (which itself
# wraps the cross-platform ``keyring`` package). They are kept as wizard-
# private names so that tests which monkeypatch ``setup_wizard._keychain_*``
# continue to work, and so the rest of the wizard reads the same regardless
# of platform.


def _keychain_set(instance_name: str, service: str, value: str) -> None:
    """Store a value in the OS credential store (create or update)."""
    _credstore.set_secret(instance_name, service, value)


def _keychain_delete(instance_name: str, service: str) -> None:
    """Delete an entry from the OS credential store. 'Not found' is ignored."""
    _credstore.delete_secret(instance_name, service)


def _keychain_get(instance_name: str, service: str) -> str | None:
    """Read a value from the OS credential store. ``None`` on failure."""
    value = _credstore.get_secret(instance_name, service)
    if value is not None:
        return value
    legacy = _legacy_macos_keychain_get(instance_name, service)
    if legacy is not None:
        _credstore.set_secret(instance_name, service, legacy)
    return legacy


def _legacy_macos_keychain_get(instance_name: str, service: str) -> str | None:
    """Read pre-v0.13.0 macOS Keychain entries and migrate on access.

    Old installs stored entries with service names like
    ``ODOO_MCP_PROD_USERNAME`` and account ``odoo-mcp-prod``. v0.13.0 moved
    to the cross-platform keyring layout ``odoo-mcp/<instance>``. This
    fallback lets existing users update without re-entering API keys.
    """
    if platform.system() != "Darwin":
        return None
    try:
        result = subprocess.run(  # noqa: S603, S607 — fixed argv, no shell
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                service,
                "-a",
                f"{_KEYCHAIN_ACCOUNT_PREFIX}{instance_name}",
                "-w",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.rstrip("\n")


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

    # Refuse to read the TOML if perms are loose. The launch path pulls
    # credentials from Keychain and injects them into ``os.environ``, and
    # ``build_app`` only runs the perms check after that — too late if the
    # config is world-readable. Apply the same gate here so the launcher
    # bails before any Keychain access.
    _check_file_permissions(DEFAULT_CONFIG_PATH)

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


def load_credentials_into_os() -> int:
    """Resolve credentials from the OS credential store into ``os.environ``.

    Replaces the legacy ``load_launch_env_into_os`` (kept as an alias below
    for backward compat). Returns 0 on success, 1 if config is missing,
    2 if any credential entry is missing. Warnings are written to stderr
    but do not abort — the server itself will surface a clearer error when
    the affected instance is touched.
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


# Backward-compat alias. ``load_launch_env_into_os`` was the v0.7.0 public
# name; v0.13.0 renames it because the implementation no longer goes via
# a launch-env shell hop.
load_launch_env_into_os = load_credentials_into_os


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


class _KeyGenError(Exception):
    """Raised by :func:`_generate_api_key_via_password` with a user-readable message."""


def _mcp_key_name(instance_name: str) -> str:
    """Stable, per-install API-key name.

    Includes the OS hostname so each user-on-machine has its own slot
    in Odoo's ``res.users.apikeys`` table. Two effects:

    - Renewals on this machine find and clean up only this machine's
      previous key; renewals on another machine never touch it.
    - When the user opens Odoo profile → Account Security, they can
      see which key belongs to which laptop at a glance.

    Falls back to ``"unknown-host"`` when ``platform.node()`` returns
    something empty / weird, so the name is always well-formed.
    """
    host = (platform.node() or "").strip() or "unknown-host"
    return f"odoo-mcp ({instance_name}) on {host}"


def _generate_api_key_via_password(
    url: str,
    database: str,
    username: str,
    password: str,
    key_name: str,
) -> tuple[str, int]:
    """Authenticate with a password, clean up stale keys, generate a fresh one.

    The single shared implementation behind both ``odoo-mcp renew-key``
    and the setup wizard's "generate the key for me" path. The password
    is used for exactly three XML-RPC calls (authenticate + create
    description + make_key) plus an optional cleanup call, and is never
    returned, logged, or stored — the caller is responsible for
    dropping its own reference afterwards.

    Generation strategy. Odoo's XML-RPC dispatcher blocks any method
    whose name starts with ``_`` (raises
    ``AccessError: "Private methods cannot be called remotely"``).
    That means the direct call to ``res.users.apikeys._generate`` —
    which earlier versions of this code attempted — was never going
    to work against a stock Odoo, including Odoo Online. We instead
    drive Odoo's own user-facing wizard:

    1. ``create`` a transient ``res.users.apikeys.description`` record
       with the desired ``name``.
    2. Call ``make_key()`` on it (no underscore → RPC-callable).
    3. The wizard returns an ``ir.actions.act_window`` whose
       ``context.default_key`` carries the freshly minted key string.

    On Odoo ≥17, ``make_key`` is decorated with ``@check_identity``,
    which requires a recent in-session credential check. Over XML-RPC
    there is no browser session, so the decorator raises. We catch
    that path and surface a clear instruction to create the key
    manually in the Odoo UI — there is no clean way to satisfy the
    identity check from this script.

    Cleanup step: before generating, searches the authenticated user's
    own keys for any row whose ``name`` equals ``key_name`` and
    unlinks them. This is best-effort — a failure here logs a warning
    and lets the renewal continue. Without it, daily renewal would
    accumulate one stale (expired-but-still-listed) key per day in
    the user's Odoo profile.

    Returns ``(new_key, num_cleaned_up)``. Raises :class:`_KeyGenError`
    with a readable message on any HARD failure (wrong password, 2FA,
    network, Odoo refusing the generate call, unexpected response).
    """
    import ssl
    import xmlrpc.client

    base = url.rstrip("/")
    ctx = ssl.create_default_context()
    common = xmlrpc.client.ServerProxy(f"{base}/xmlrpc/2/common", context=ctx, allow_none=True)
    try:
        uid = common.authenticate(database, username, password, {})
    except xmlrpc.client.Fault as exc:
        raise _KeyGenError(
            f"Odoo rejected the password: {exc.faultString}\n"
            f"  Common causes: wrong password, or 2FA enabled (password auth "
            f"is then blocked — create the key manually in your Odoo profile)."
        ) from exc
    except (OSError, ssl.SSLError, TimeoutError) as exc:
        raise _KeyGenError(f"Could not reach Odoo: {type(exc).__name__}: {exc}") from exc

    if not isinstance(uid, int) or not uid:
        raise _KeyGenError("Authentication returned no uid. Check the username and database name.")

    models = xmlrpc.client.ServerProxy(f"{base}/xmlrpc/2/object", context=ctx, allow_none=True)

    # --- Cleanup (best-effort) -------------------------------------------
    # Find this user's own existing keys with the exact same name and
    # unlink them. ``user_id`` filter is defence in depth — Odoo's ACL
    # on res.users.apikeys should restrict users to their own rows
    # anyway, but pinning it makes the intent explicit and survives
    # a future ACL regression.
    num_cleaned = 0
    try:
        existing_ids = models.execute_kw(
            database,
            uid,
            password,
            "res.users.apikeys",
            "search",
            [[("name", "=", key_name), ("user_id", "=", uid)]],
            {},
        )
        if isinstance(existing_ids, list) and existing_ids:
            models.execute_kw(
                database,
                uid,
                password,
                "res.users.apikeys",
                "unlink",
                [existing_ids],
                {},
            )
            num_cleaned = len(existing_ids)
    except (xmlrpc.client.Fault, OSError, ssl.SSLError, TimeoutError) as exc:
        logger.warning(
            "Could not clean up old API keys for %r on %r before renewal: "
            "%s: %s. The new key will still be generated; old keys remain "
            "in your Odoo profile until you delete them manually.",
            username,
            base,
            type(exc).__name__,
            exc,
        )

    # --- Generate the new key via Odoo's own description wizard ----------
    # The wizard model is a TransientModel; orphan records are GC'd by
    # Odoo's regular vacuum, so we don't need to unlink on failure.
    try:
        desc_id = models.execute_kw(
            database,
            uid,
            password,
            "res.users.apikeys.description",
            "create",
            [{"name": key_name}],
            {},
        )
    except xmlrpc.client.Fault as exc:
        raise _KeyGenError(_format_keygen_fault(exc.faultString)) from exc
    except (OSError, ssl.SSLError, TimeoutError) as exc:
        raise _KeyGenError(f"Could not generate key: {type(exc).__name__}: {exc}") from exc

    if not isinstance(desc_id, int) or desc_id <= 0:
        raise _KeyGenError(f"Unexpected response creating the API-key wizard record: {desc_id!r}")

    try:
        action = models.execute_kw(
            database,
            uid,
            password,
            "res.users.apikeys.description",
            "make_key",
            [[desc_id]],
            {},
        )
    except xmlrpc.client.Fault as exc:
        raise _KeyGenError(_format_keygen_fault(exc.faultString)) from exc
    except (OSError, ssl.SSLError, TimeoutError) as exc:
        raise _KeyGenError(f"Could not generate key: {type(exc).__name__}: {exc}") from exc

    new_key = _extract_key_from_make_key_result(action)
    if new_key is None:
        raise _KeyGenError(
            "Odoo accepted the request but did not return the new key in the "
            "expected shape. This usually means your Odoo version returns the "
            "key in a wizard form rather than the action context — create the "
            "key manually in your Odoo profile (Account Security → New API Key) "
            "and rerun this command choosing option 1."
        )
    return new_key, num_cleaned


def _format_keygen_fault(fault_string: str) -> str:
    """Turn a raw Odoo XML-RPC fault into an actionable user-readable message.

    The three failure modes we've actually seen in the field:

    - "Private methods (...) cannot be called remotely" — legacy code path
      against an old odoo-mcp; the user is on a version that still tries
      ``_generate`` directly. Won't trigger from this function anymore,
      but kept as a hint in case a future Odoo blocks ``make_key`` too.
    - ``@check_identity`` rejection on Odoo ≥17 — the wizard demands a
      recent in-session credential check that XML-RPC can't provide.
    - Anything else: surface the raw message and point at the manual path.
    """
    low = fault_string.lower()
    if "private method" in low and "cannot be called remotely" in low:
        return (
            f"Odoo refused the API call: {fault_string}\n"
            "  Your odoo-mcp is calling a private method directly. Upgrade "
            "to the latest release (`odoo-mcp update`) — newer versions use "
            "Odoo's user-facing wizard instead."
        )
    if "identity" in low or "re-enter" in low or "reauthenticat" in low:
        return (
            f"Odoo requires an in-session identity re-check that cannot be "
            f"performed over the API: {fault_string}\n"
            "  Create the key manually:\n"
            "    1. Open Odoo → top-right menu → My Profile → Account Security.\n"
            "    2. Click 'New API Key', name it however you like, copy the key.\n"
            "    3. Rerun this command and choose option 1 to paste it."
        )
    return (
        f"Odoo refused to generate a new key: {fault_string}\n"
        "  Create the key manually in your Odoo profile "
        "(Account Security → New API Key) and rerun this command "
        "choosing option 1 to paste it."
    )


def _extract_key_from_make_key_result(action: object) -> str | None:
    """Pull the new API key out of ``res.users.apikeys.description.make_key()``.

    Odoo's wizard returns either:

    - an ``ir.actions.act_window`` dict whose ``context.default_key``
      (or, in some forks, ``context.default_key_value``) holds the key;
    - or, on very old Odoo versions, the raw key string.

    Returns the key string or None if the shape isn't recognised. The
    caller turns None into an actionable error.
    """
    if isinstance(action, str) and action:
        return action
    if isinstance(action, dict):
        ctx = action.get("context")
        if isinstance(ctx, dict):
            for field in ("default_key", "default_key_value"):
                value = ctx.get(field)
                if isinstance(value, str) and value:
                    return value
    return None


def _ask_api_key(url: str, database: str, username: str, instance_name: str) -> str:
    """Collect an API key — either pasted, or generated from a password.

    Offers two paths. Path 1 keeps the historical behaviour (paste a key
    you created in the Odoo UI). Path 2 is the low-friction default:
    type your Odoo password once, the wizard generates the key via
    :func:`_generate_api_key_via_password` and discards the password.
    Path 2 falls through to a clear message for 2FA users (who must use
    path 1, since 2FA blocks password auth).
    """
    print()
    print("How do you want to authenticate this instance?")
    print("  1) Paste an API key you create yourself in Odoo's profile UI.")
    print("  2) Type your Odoo password once — the wizard generates the key")
    print("     for you and discards the password. (Recommended; not for")
    print("     accounts with 2FA enabled.)")
    choice = _ask("Choice", default="2")

    if choice.strip() == "1":
        api_key = getpass.getpass("API key (will not echo): ").strip()
        if not api_key:
            print("API key cannot be empty.")
            sys.exit(1)
        return api_key

    # Path 2 — generate via password.
    password = getpass.getpass("Odoo password (will not echo, not stored): ")
    try:
        if not password:
            print("Password cannot be empty.")
            sys.exit(1)
        try:
            key, num_cleaned = _generate_api_key_via_password(
                url, database, username, password, _mcp_key_name(instance_name)
            )
        except _KeyGenError as exc:
            print(f"  {exc}")
            print("  Falling back to manual entry.")
            api_key = getpass.getpass("API key (will not echo): ").strip()
            if not api_key:
                print("API key cannot be empty.")
                sys.exit(1)
            return api_key
    finally:
        password = ""  # noqa: F841 — deliberate disposal
    if num_cleaned:
        print(f"  ✓ Removed {num_cleaned} stale API key(s) on this machine.")
    print("  ✓ API key generated and will be stored in the OS credential store.")
    return key


def _ask_instance() -> dict[str, str | bool]:
    """Interactively collect instance details. Returns a dict."""
    name = _ask("Instance name", default="main", validator="name")
    url = _ask("Odoo URL (https://... or http://...)", validator="url")
    database = _ask("Database name")
    production = _ask_bool("Is this a production instance?", default=True)
    username = _ask("Username (email)")
    api_key = _ask_api_key(url, database, username, name)
    return {
        "name": name,
        "url": url,
        "database": database,
        "production": production,
        "username": username,
        "api_key": api_key,
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


def _resolve_odoo_mcp_command() -> str:
    """Return the absolute path to the ``odoo-mcp`` CLI for Claude Desktop.

    Prefers the entry point installed by ``uv tool install`` (on PATH as
    ``odoo-mcp`` / ``odoo-mcp.exe``). Falls back to ``sys.executable -m
    odoo_mcp`` form is NOT used here because Claude Desktop's ``command``
    only takes one program — for that fallback we'd need an installed
    launcher script. If we cannot find ``odoo-mcp`` on PATH we still
    register the bare name and let the user fix their PATH; the wizard
    prints a hint in that case.
    """
    found = shutil.which("odoo-mcp")
    if found:
        return found
    # Last-resort: register the name and trust the user's PATH at runtime.
    print(
        "  Warning: 'odoo-mcp' not found on PATH. Registering by name; "
        "ensure ~/.local/bin (POSIX) or %USERPROFILE%\\.local\\bin (Windows) "
        "is on your PATH before launching Claude Desktop."
    )
    return "odoo-mcp"


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
        "command": _resolve_odoo_mcp_command(),
        "args": ["launch"],
    }

    _atomic_write_text(
        _CLAUDE_DESKTOP_CONFIG,
        json.dumps(config, indent=2) + "\n",
        mode=0o600,
    )
    print(f"  Registered in Claude Desktop config: {_CLAUDE_DESKTOP_CONFIG}")


def _replace_toml_table(content: str, table: str, values: dict[str, object]) -> str:
    """Replace or append a flat TOML table while preserving the rest of the file."""
    header = f"[{table}]"
    lines = content.splitlines()
    out: list[str] = []
    i = 0
    replaced = False

    while i < len(lines):
        if lines[i].strip() == header:
            replaced = True
            first = True
            while i < len(lines):
                stripped = lines[i].strip()
                if not first and stripped.startswith("[") and stripped.endswith("]"):
                    break
                first = False
                i += 1
            if out and out[-1].strip():
                out.append("")
            out.extend(_render_toml_table(header, values))
            if i < len(lines) and lines[i].strip():
                out.append("")
            continue
        out.append(lines[i])
        i += 1

    if not replaced:
        while out and not out[-1].strip():
            out.pop()
        if out:
            out.append("")
            out.append("")
        out.extend(_render_toml_table(header, values))

    return "\n".join(out).rstrip() + "\n"


def _render_toml_table(header: str, values: dict[str, object]) -> list[str]:
    lines = [header]
    for key, value in values.items():
        lines.append(f"{key} = {_toml_value(value)}")
    return lines


def _codex_available() -> bool:
    """Return True when this machine appears to use Codex."""
    return _CODEX_CONFIG.exists() or bool(shutil.which("codex"))


def _register_codex() -> bool:
    """Add odoo-mcp to Codex config.toml (atomic write).

    Returns True when a registration was written. If Codex is not installed
    and no config exists, this is a no-op so Claude-only users are not given
    an unexpected ``~/.codex`` directory.
    """
    if not _codex_available():
        return False
    content = ""
    if _CODEX_CONFIG.exists():
        try:
            content = _CODEX_CONFIG.read_text()
        except OSError as exc:
            print(
                f"  Warning: could not read {_CODEX_CONFIG}; skipping Codex registration ({exc})."
            )
            return False

    updated = _replace_toml_table(
        content,
        "mcp_servers.odoo-mcp",
        {
            "command": _resolve_odoo_mcp_command(),
            "args": ["launch"],
        },
    )
    _atomic_write_text(_CODEX_CONFIG, updated, mode=0o600)
    print(f"  Registered in Codex config: {_CODEX_CONFIG}")
    return True


def _register_clients() -> None:
    print("\nRegistering in Claude Desktop...")
    _register_claude_desktop()
    print("\nRegistering in Codex...")
    if not _register_codex():
        print("  Codex not detected; skipped Codex registration.")


def _run_doctor() -> None:
    """Run doctor checks inline."""
    print("\nRunning doctor checks...")
    from .doctor import run_doctor

    run_doctor()


def _acknowledge_admin_or_abort(name: str) -> bool:
    """Detect admin credentials on a fresh production instance and offer a choice.

    Runs after the config has been written but before doctor. If the API
    key authenticates as Odoo admin on a ``production = true`` instance,
    the default-strict ``refuse_admin_on_production`` would block doctor
    and every subsequent tool call. Many operators only have admin keys
    available (especially on small Odoo SaaS deployments), so we offer
    them an interactive opt-out instead of a silent failure.

    Returns ``True`` when setup can continue (non-admin, non-prod, or the
    user explicitly acknowledged). Returns ``False`` when the user chose
    to abort and create a non-admin user first. The opt-out is
    persisted by editing the instance's TOML block in place; the next
    doctor run sees ``refuse_admin_on_production = false`` and the
    warning is downgraded to informational.

    Best-effort: any unexpected exception falls through with a warning
    rather than aborting the wizard — doctor will still surface the
    same situation, just less gracefully.
    """
    try:
        from .client import OdooClient
        from .config import load_config
        from .credentials import load_credentials
        from .errors import OdooAuthError

        cfg = load_config(DEFAULT_CONFIG_PATH)
        instance = cfg.instances.get(name)
        if instance is None:
            return True
        if not instance.production:
            # Admin on a dev instance is fine — only the prod gate matters.
            return True
        if not instance.refuse_admin_on_production:
            # Already acknowledged in a previous run; nothing to do.
            return True

        creds = load_credentials(instance.name, instance.credentials_env_prefix)
        client = OdooClient(instance, credentials=creds)
        # Catch the OdooAuthError that the strict-by-default refusal would
        # raise. We're in the wizard precisely to handle this gracefully.
        try:
            client.authenticate()
        except OdooAuthError as exc:
            if "Refusing to use admin credentials" not in exc.user_message:
                # Some other auth problem — let the wizard's normal flow
                # surface it via doctor.
                return True
            # Admin detected. Offer the choice.
            print()
            print("=" * 60)
            print(f"  Admin-credentials detected on production instance {name!r}")
            print("=" * 60)
            print()
            print("This API key has Odoo system-administrator rights")
            print("(uid=1 or member of base.group_system). Most Odoo")
            print("record rules are bypassed by such users, which removes")
            print("the per-user ACL scoping this MCP relies on.")
            print()
            print("Two options:")
            print()
            print("  A) RECOMMENDED — abort this setup, create a")
            print("     dedicated non-admin Odoo user with only the groups")
            print("     it needs (Sales / Accounting / etc), then rerun")
            print("     'odoo-mcp setup' with that user's API key.")
            print()
            print("  B) ACKNOWLEDGE THE RISK — proceed with the admin key.")
            print("     The MCP's client-side denylist, redaction, and")
            print("     prod-write guard still apply; only Odoo's per-user")
            print("     record rules are bypassed. This sets")
            print("     'refuse_admin_on_production = false' for this")
            print("     instance in your config.")
            print()
            choice = _ask("Type 'acknowledge' to proceed with admin, anything else to abort")
            if choice.strip().lower() != "acknowledge":
                print()
                print("Aborted. To rerun: 'odoo-mcp setup' (after creating")
                print("a non-admin user), or 'odoo-mcp setup --remove' to")
                print(f"clean up the {name!r} entry first.")
                return False
            _persist_admin_acknowledgment(name)
            print()
            print(f"  Acknowledged. refuse_admin_on_production = false written for {name!r}.")
            return True

        # Authenticate succeeded — either non-admin or admin on dev. The
        # admin-warning surface in doctor will still flag it for visibility.
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort, mustn't abort wizard
        logger.debug("admin acknowledgment check skipped: %s", exc)
        return True


def _persist_admin_acknowledgment(name: str) -> None:
    """Edit ``[instances.NAME]`` in config.toml to add ``refuse_admin_on_production = false``."""
    defaults, instances = _load_raw_config()
    if name not in instances:
        return
    instances[name]["refuse_admin_on_production"] = False
    _write_config(defaults, instances)


def _cmd_acknowledge_admin(name: str) -> int:
    """Repair an existing config that's stuck on the admin-refusal gate.

    The companion to :func:`_acknowledge_admin_or_abort`: that one runs
    inside the wizard before doctor; this one is the standalone fix for
    a user who already wrote a config (perhaps under v0.15.4 or earlier)
    and hit the wall on ``odoo-mcp doctor`` or first tool call. Same
    persistent effect: writes ``refuse_admin_on_production = false`` to
    the instance's TOML block. Idempotent.
    """
    if not DEFAULT_CONFIG_PATH.exists():
        print(f"No config found at {DEFAULT_CONFIG_PATH}")
        print("Run 'odoo-mcp setup' first.")
        return 1
    defaults, instances = _load_raw_config()
    if name not in instances:
        print(f"Instance {name!r} not found. Configured: {sorted(instances.keys())}")
        return 1

    print()
    print("=" * 60)
    print(f"  Acknowledging admin credentials on instance {name!r}")
    print("=" * 60)
    print()
    print("Setting 'refuse_admin_on_production = false' for this instance.")
    print()
    print("Reminder: the MCP's client-side denylist, redaction, and")
    print("prod-write guard still apply. Only Odoo's per-user record")
    print("rules — which would normally scope what this user can read")
    print("and write — are bypassed because this API key is admin.")
    print()
    print("If you'd rather not bypass them, the right fix is to create")
    print("a non-admin Odoo user with the groups it needs and rotate")
    print(f"via 'odoo-mcp setup --rotate-key {name}'.")
    print()
    confirm = _ask("Type 'acknowledge' to persist, anything else to abort")
    if confirm.strip().lower() != "acknowledge":
        print("\nAborted. No changes made.")
        return 1

    instances[name]["refuse_admin_on_production"] = False
    _write_config(defaults, instances)
    print(f"\n  Updated {DEFAULT_CONFIG_PATH}")
    print(f"  refuse_admin_on_production = false written for {name!r}.")
    print()
    print("Run 'odoo-mcp doctor' to verify the instance now authenticates.")
    return 0


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
    print(f"  Instance:     {name} ({url})")
    print("\nNext steps:")
    print("  1. Restart Claude Desktop / Cowork and Codex to pick up the new MCP.")
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

    print("\nRegistering in Claude Desktop...")
    _register_claude_desktop()

    if not _acknowledge_admin_or_abort(name):
        return 1
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

    if not _acknowledge_admin_or_abort(name):
        return 1
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
# Subcommand: renew-key NAME (password-auth → programmatic key generation)
# ---------------------------------------------------------------------------


def _cmd_renew_key(name: str) -> int:
    """Generate a fresh API key via password authentication.

    Built specifically for Odoo Online, where non-admin API keys expire
    after 1 day by platform policy. Cannot be relaxed via custom module
    (Odoo Online forbids custom modules) or via System Parameters
    (Odoo enforces the policy at the platform level).

    Flow:

      1. Read the instance config; find URL + database + username.
      2. Prompt for the user's Odoo password (NOT echoed, NOT stored).
      3. Authenticate to Odoo via password — this is allowed for users
         who don't have 2FA enabled.
      4. Once authenticated, drive Odoo's own ``API Key Description``
         wizard (``res.users.apikeys.description.make_key``) to produce
         a fresh key — the same path the Account Security UI uses.
      5. Write the new key to the OS credential store, overwriting
         the previous (typically expired) one.
      6. Disposal: the password variable is overwritten and dropped
         from the local scope. Python doesn't let us forcibly wipe
         strings from memory, so the structural mitigation is "use
         it once, then drop the reference".

    Returns 0 on success, 1 on any failure (with a readable message).
    """
    if not DEFAULT_CONFIG_PATH.exists():
        print(f"No config found at {DEFAULT_CONFIG_PATH}")
        return 1
    _, instances = _load_raw_config()
    if name not in instances:
        print(f"Instance {name!r} is not configured.")
        print(f"Known instances: {sorted(instances.keys())}")
        return 1

    inst = instances[name]
    url = str(inst.get("url") or "")
    database = str(inst.get("database") or "")
    prefix = str(inst.get("credentials_env_prefix") or _env_prefix(name))
    username_service = f"{prefix}_USERNAME"
    api_key_service = f"{prefix}_API_KEY"
    username = _keychain_get(name, username_service)

    if not url or not database:
        print(f"Instance {name!r} has incomplete config (url / database missing).")
        return 1
    if not username:
        print(f"No username found in keychain for instance {name!r}.")
        print("Run 'odoo-mcp setup --add' to (re)configure it.")
        return 1

    print(f"Renewing API key for {username} on instance '{name}'.")
    print("Your Odoo password is used once to generate a new key and then")
    print("discarded. It is NOT stored anywhere.")
    print()
    password = getpass.getpass("Odoo password (will not echo): ")
    try:
        if not password:
            print("Password cannot be empty. Aborted.")
            return 1
        try:
            new_key, num_cleaned = _generate_api_key_via_password(
                url, database, username, password, _mcp_key_name(name)
            )
        except _KeyGenError as exc:
            print(f"  {exc}")
            return 1
    finally:
        # Structural disposal: drop the password reference immediately.
        # Python's GC will collect it on the next cycle; we can't force
        # it but we can keep the window small.
        password = ""  # noqa: F841 — deliberate overwrite

    _keychain_set(name, api_key_service, new_key)
    print()
    if num_cleaned:
        print(f"  ✓ Removed {num_cleaned} stale API key(s) on this machine.")
    print(f"  ✓ New API key stored in keychain for instance '{name}'.")
    print("  Existing Claude Desktop / Codex sessions will pick it up after restart.")
    print()
    print("  On Odoo Online, this new key is typically valid for 1 day.")
    print("  Re-run 'odoo-mcp renew-key' tomorrow morning.")
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


def _remove_odoo_mcp_from_codex() -> bool:
    """Strip the ``odoo-mcp`` entry from Codex config.toml (atomic)."""
    if not _CODEX_CONFIG.exists():
        return False
    try:
        content = _CODEX_CONFIG.read_text()
    except OSError:
        print(f"  Warning: could not read {_CODEX_CONFIG}; leaving it alone.")
        return False

    header = "[mcp_servers.odoo-mcp]"
    lines = content.splitlines()
    out: list[str] = []
    i = 0
    removed = False
    while i < len(lines):
        if lines[i].strip() == header:
            removed = True
            first = True
            while i < len(lines):
                stripped = lines[i].strip()
                if not first and stripped.startswith("[") and stripped.endswith("]"):
                    break
                first = False
                i += 1
            while out and not out[-1].strip():
                out.pop()
            if i < len(lines) and out:
                out.append("")
            continue
        out.append(lines[i])
        i += 1

    if not removed:
        return False
    _atomic_write_text(_CODEX_CONFIG, "\n".join(out).rstrip() + "\n", mode=0o600)
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
    print(f"  - The 'odoo-mcp' entry in {_CODEX_CONFIG}")
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

    print("\nRemoving Codex registration...")
    if _remove_odoo_mcp_from_codex():
        print(f"  Removed odoo-mcp entry from {_CODEX_CONFIG}")
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
    print("Restart Claude Desktop / Cowork and Codex to drop now-stale MCP entries.")
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
        ack = _extract_flag_value(args, "--acknowledge-admin")
        if ack is not None:
            if not ack:
                print("Usage: odoo-mcp setup --acknowledge-admin NAME")
                return 2
            return _cmd_acknowledge_admin(ack)
        if "--add" in args:
            return _cmd_add()
        if "--remove" in args:
            return _cmd_remove()
        return _cmd_setup()
    except KeyboardInterrupt:
        print("\n\nSetup cancelled.")
        return 130
