"""Config inspection CLI.

Usage::

    odoo-mcp config show                # dump the effective config (sanitized)
    odoo-mcp config validate [PATH]     # parse and validate config; exit 0 if OK

``show`` never prints credential values — only a presence check against the
macOS Keychain. ``validate`` never authenticates or reads credentials; it only
runs :func:`odoo_mcp.config.load_config` and reports the outcome.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH, AppConfig, InstanceConfig, load_config
from .errors import ConfigError
from .security.allowlist import ALLOWLIST_WILDCARD, MODEL_DENYLIST
from .setup_wizard import _keychain_get


def main(argv: list[str]) -> int:
    """Dispatch ``config`` subcommands. Returns a process exit code."""
    if not argv:
        _print_usage()
        return 2
    sub = argv[0]
    rest = argv[1:]
    if sub == "show":
        return _cmd_show()
    if sub == "validate":
        return _cmd_validate(rest)
    _print_usage()
    return 2


def _print_usage() -> None:
    print(
        "Usage:\n  odoo-mcp config show\n  odoo-mcp config validate [PATH]",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def _cmd_show() -> int:
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"ConfigError: {exc}", file=sys.stderr)
        return 1

    lines = _render_show(cfg, check_keychain=True)
    print("\n".join(lines))
    return 0


def _render_show(cfg: AppConfig, *, check_keychain: bool) -> list[str]:
    """Render the resolved config block. Returns lines (no trailing newline)."""
    lines: list[str] = []
    lines.append(f"Config file:    {cfg.path}")
    lines.append(f"Audit log:      {cfg.audit_log_path}")
    lines.append("")
    lines.append("Defaults")
    lines.append("--------")
    d = cfg.defaults
    lines.append(f"timeout_seconds:       {d.timeout_seconds}")
    lines.append(f"max_records_default:   {d.max_records_default}")
    lines.append(f"max_records_hard_cap:  {d.max_records_hard_cap}")
    lines.append(f"allowed_models:        {_format_allowlist(list(d.allowed_models))}")
    lines.append(f"denylist:              {len(MODEL_DENYLIST)} models (always blocked)")

    global_models = frozenset(d.allowed_models)
    for name, inst in cfg.instances.items():
        lines.append("")
        lines.append(f"Instance: {name}")
        lines.append("-" * (len(f"Instance: {name}")))
        lines.extend(_render_instance(inst, global_models, check_keychain=check_keychain))
    return lines


def _render_instance(
    inst: InstanceConfig,
    global_models: frozenset[str],
    *,
    check_keychain: bool,
) -> list[str]:
    lines: list[str] = []
    lines.append(f"url:                     {inst.url}")
    lines.append(f"database:                {inst.database}")
    lines.append(f"production:              {_bool(inst.production)}")
    lines.append(f"credentials_env_prefix:  {inst.credentials_env_prefix}")
    lines.append(f"credentials_status:      {_credentials_status(inst, check_keychain)}")
    lines.append(f"timeout_seconds:         {inst.timeout_seconds}")
    lines.append(f"rate_limit_per_minute:   {inst.rate_limit_per_minute}")
    lines.append(f"allow_self_signed:       {_bool(inst.allow_self_signed)}")

    # allowed_models: show full list only when overridden. Open mode gets a
    # one-line summary that includes the denylist size.
    if ALLOWLIST_WILDCARD in inst.allowed_models:
        lines.append(
            f"allowed_models:          open mode ({len(MODEL_DENYLIST)} models in denylist)"
        )
    elif inst.allowed_models == global_models:
        lines.append(
            f"allowed_models:          ({len(inst.allowed_models)} total, using global defaults)"
        )
    else:
        lines.append(f"allowed_models:          {_format_models_full(sorted(inst.allowed_models))}")

    # sensitive_fields: show override contents only when set.
    if not inst.sensitive_fields:
        lines.append("sensitive_fields_override: (none, using global defaults)")
    else:
        lines.append("sensitive_fields_override:")
        for model in sorted(inst.sensitive_fields.keys()):
            names = sorted(inst.sensitive_fields[model])
            lines.append(f"  {model}: [{', '.join(names)}]")
    return lines


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _format_allowlist(models: list[str]) -> str:
    """Render the ``[defaults].allowed_models`` value, handling open mode."""
    if ALLOWLIST_WILDCARD in models:
        return f"open mode ({len(MODEL_DENYLIST)} models in denylist)"
    return _format_models_short(models)


def _format_models_short(models: list[str]) -> str:
    """Render an unmodified (global-default) allow_models list compactly."""
    if not models:
        return "[]"
    preview = ", ".join(models[:2])
    total = len(models)
    if total <= 2:
        return f"[{preview}]"
    return f"[{preview}, ... ({total} total)]"


def _format_models_full(models: list[str]) -> str:
    """Render an instance-overridden allow_models list in full."""
    if not models:
        return "[]"
    inner = ", ".join(models)
    return f"[{inner}]  ({len(models)} total, instance override)"


def _credentials_status(inst: InstanceConfig, check_keychain: bool) -> str:
    """Report Keychain presence without ever revealing the value."""
    if not check_keychain:
        return "not checked"
    username = _keychain_get(inst.name, f"{inst.credentials_env_prefix}_USERNAME")
    api_key = _keychain_get(inst.name, f"{inst.credentials_env_prefix}_API_KEY")
    if username and api_key:
        return "present in Keychain"
    missing: list[str] = []
    if not username:
        missing.append("USERNAME")
    if not api_key:
        missing.append("API_KEY")
    return f"missing ({', '.join(missing)})"


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def _cmd_validate(rest: list[str]) -> int:
    if len(rest) > 1:
        _print_usage()
        return 2
    path: Path | None = Path(rest[0]).expanduser() if rest else None
    target = path if path is not None else DEFAULT_CONFIG_PATH

    try:
        cfg = load_config(path)
    except ConfigError as exc:
        print(f"\u2717 ConfigError: {exc}", file=sys.stderr)
        return 1

    print(f"\u2713 Config valid: {target}")
    names = sorted(cfg.instances.keys())
    print(f"Instances ({len(names)}): {', '.join(names)}")
    return 0
