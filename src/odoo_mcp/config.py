"""TOML config loader for the Odoo MCP.

The config file lives at ``~/.odoo-mcp/config.toml`` by default. It contains
**only non-secret metadata** (URLs, database names, allowlists, timeouts).
Secrets come from env vars via :mod:`odoo_mcp.credentials`.

Security rules enforced here:

1. The file must exist and be a regular file.
2. The file's mode must be ``0o600`` (owner read/write only). If group or
   other has any bits set, startup refuses.
3. TOML parse errors raise :class:`ConfigError`.
4. Unknown top-level keys are rejected (typo protection — a misspelled
   ``production`` field should be loud, not silent).
5. Defaults are filled in from :class:`Defaults`, and per-instance overrides
   must be valid types.
"""

from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from .errors import ConfigError

DEFAULT_CONFIG_PATH: Final[Path] = Path("~/.odoo-mcp/config.toml").expanduser()
DEFAULT_AUDIT_LOG: Final[str] = "~/.odoo-mcp/audit.jsonl"

_DEFAULT_ALLOWED_MODELS: Final[tuple[str, ...]] = (
    "res.partner",
    "crm.lead",
    "crm.team",
    "sale.order",
    "sale.order.line",
    "product.product",
    "product.template",
    "account.move",
    "account.move.line",
    "account.payment",
    "project.project",
    "project.task",
    "hr.employee",
    "hr.leave",
)

_VALID_DEFAULT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "timeout_seconds",
        "max_records_default",
        "max_records_hard_cap",
        "audit_log",
        "allowed_models",
    }
)

_VALID_INSTANCE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "url",
        "database",
        "credentials_env_prefix",
        "production",
        "rate_limit_per_minute",
        "allow_self_signed",
        "timeout_seconds",
        "max_records_default",
        "max_records_hard_cap",
        "allowed_models",
    }
)


@dataclass(frozen=True, slots=True)
class Defaults:
    timeout_seconds: int = 30
    max_records_default: int = 50
    max_records_hard_cap: int = 500
    audit_log: str = DEFAULT_AUDIT_LOG
    allowed_models: tuple[str, ...] = _DEFAULT_ALLOWED_MODELS


@dataclass(frozen=True, slots=True)
class InstanceConfig:
    name: str
    url: str
    database: str
    credentials_env_prefix: str
    production: bool
    timeout_seconds: int
    max_records_default: int
    max_records_hard_cap: int
    rate_limit_per_minute: int
    allow_self_signed: bool
    allowed_models: frozenset[str]


@dataclass(frozen=True, slots=True)
class AppConfig:
    path: Path
    defaults: Defaults
    instances: dict[str, InstanceConfig] = field(default_factory=dict)
    audit_log_path: Path = field(default_factory=lambda: Path(DEFAULT_AUDIT_LOG).expanduser())


def load_config(path: Path | None = None) -> AppConfig:
    """Load and validate the config file.

    ``path`` defaults to ``~/.odoo-mcp/config.toml``. Raises
    :class:`ConfigError` on any problem.
    """
    cfg_path = (path or DEFAULT_CONFIG_PATH).expanduser()

    if not cfg_path.exists():
        raise ConfigError(f"Config file not found: {cfg_path}")
    if not cfg_path.is_file():
        raise ConfigError(f"Config path is not a regular file: {cfg_path}")

    _check_file_permissions(cfg_path)

    try:
        with cfg_path.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Could not parse config TOML at {cfg_path}: {exc}") from exc

    defaults = _parse_defaults(raw.get("defaults", {}))
    instances = _parse_instances(raw.get("instances", {}), defaults)

    if not instances:
        raise ConfigError("No [instances.*] entries configured — nothing for the MCP to do.")

    audit_log_path = Path(defaults.audit_log).expanduser()

    return AppConfig(
        path=cfg_path,
        defaults=defaults,
        instances=instances,
        audit_log_path=audit_log_path,
    )


def _check_file_permissions(path: Path) -> None:
    """Refuse to load a config file with loose permissions.

    Owner r/w is fine (0o600 or 0o400). Any group or other bit set is fatal.
    On platforms where ``st_mode`` is unreliable (Windows) we skip the check.
    """
    if os.name != "posix":
        return
    st = path.stat()
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise ConfigError(
            f"Config file {path} has loose permissions "
            f"(mode {oct(stat.S_IMODE(st.st_mode))}). "
            f"Run: chmod 600 {path}"
        )


def _parse_defaults(raw: dict[str, Any]) -> Defaults:
    _reject_unknown_keys(raw, _VALID_DEFAULT_KEYS, "defaults")
    return Defaults(
        timeout_seconds=_require_int(raw, "timeout_seconds", 30, minimum=1, maximum=120),
        max_records_default=_require_int(
            raw, "max_records_default", 50, minimum=1, maximum=1000
        ),
        max_records_hard_cap=_require_int(
            raw, "max_records_hard_cap", 500, minimum=1, maximum=10_000
        ),
        audit_log=str(raw.get("audit_log", DEFAULT_AUDIT_LOG)),
        allowed_models=tuple(_require_str_list(raw, "allowed_models", _DEFAULT_ALLOWED_MODELS)),
    )


def _parse_instances(
    raw: dict[str, Any], defaults: Defaults
) -> dict[str, InstanceConfig]:
    out: dict[str, InstanceConfig] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            raise ConfigError(f"[instances.{name}] must be a table, got {type(entry).__name__}")
        _reject_unknown_keys(entry, _VALID_INSTANCE_KEYS, f"instances.{name}")

        if "url" not in entry or not isinstance(entry["url"], str):
            raise ConfigError(f"[instances.{name}].url is required and must be a string")
        url = entry["url"]
        if not url.startswith("https://") and not url.startswith("http://"):
            raise ConfigError(
                f"[instances.{name}].url must start with http:// or https:// (got {url!r})"
            )

        production = bool(entry.get("production", False))
        if production and not url.startswith("https://"):
            raise ConfigError(
                f"[instances.{name}] is marked production but url is not HTTPS. Refusing."
            )

        if "database" not in entry or not isinstance(entry["database"], str):
            raise ConfigError(f"[instances.{name}].database is required and must be a string")
        database = entry["database"]

        if "credentials_env_prefix" not in entry or not isinstance(
            entry["credentials_env_prefix"], str
        ):
            raise ConfigError(
                f"[instances.{name}].credentials_env_prefix is required and must be a string"
            )
        env_prefix = entry["credentials_env_prefix"]
        if not env_prefix.replace("_", "").isalnum():
            raise ConfigError(
                f"[instances.{name}].credentials_env_prefix must be alphanumeric + underscore"
            )

        allow_self_signed = bool(entry.get("allow_self_signed", False))
        if production and allow_self_signed:
            raise ConfigError(
                f"[instances.{name}] cannot set allow_self_signed on a production instance"
            )

        default_rate = 60 if production else 300
        rate_limit = _require_int(
            entry, "rate_limit_per_minute", default_rate, minimum=1, maximum=10_000
        )

        timeout = _require_int(
            entry,
            "timeout_seconds",
            defaults.timeout_seconds,
            minimum=1,
            maximum=120,
        )
        max_def = _require_int(
            entry,
            "max_records_default",
            defaults.max_records_default,
            minimum=1,
            maximum=defaults.max_records_hard_cap,
        )
        max_cap = _require_int(
            entry,
            "max_records_hard_cap",
            defaults.max_records_hard_cap,
            minimum=max_def,
            maximum=10_000,
        )

        models = frozenset(
            _require_str_list(entry, "allowed_models", list(defaults.allowed_models))
        )
        if not models:
            raise ConfigError(f"[instances.{name}].allowed_models cannot be empty")

        out[name] = InstanceConfig(
            name=name,
            url=url.rstrip("/"),
            database=database,
            credentials_env_prefix=env_prefix,
            production=production,
            timeout_seconds=timeout,
            max_records_default=max_def,
            max_records_hard_cap=max_cap,
            rate_limit_per_minute=rate_limit,
            allow_self_signed=allow_self_signed,
            allowed_models=models,
        )
    return out


def _reject_unknown_keys(
    raw: dict[str, Any], valid: frozenset[str], section: str
) -> None:
    unknown = set(raw.keys()) - valid
    if unknown:
        raise ConfigError(
            f"Unknown keys in [{section}]: {sorted(unknown)}. "
            f"Valid keys: {sorted(valid)}"
        )


def _require_int(
    raw: dict[str, Any],
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key!r} must be an integer, got {type(value).__name__}")
    int_value: int = value
    if int_value < minimum or int_value > maximum:
        raise ConfigError(f"{key!r} must be in [{minimum}, {maximum}], got {int_value}")
    return int_value


def _require_str_list(
    raw: dict[str, Any], key: str, default: list[str] | tuple[str, ...]
) -> list[str]:
    value = raw.get(key, list(default))
    if not isinstance(value, list):
        raise ConfigError(f"{key!r} must be a list of strings")
    for item in value:
        if not isinstance(item, str):
            raise ConfigError(f"{key!r} entries must be strings, got {type(item).__name__}")
    return list(value)
