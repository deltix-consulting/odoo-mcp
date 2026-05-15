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
import re
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from .errors import ConfigError

DEFAULT_CONFIG_PATH: Final[Path] = Path("~/.odoo-mcp/config.toml").expanduser()
DEFAULT_AUDIT_LOG: Final[str] = "~/.odoo-mcp/audit.jsonl"
DEFAULT_FIELDS_CACHE: Final[str] = "~/.odoo-mcp/fields-cache.db"

# Default allowlist since v0.4.0: the sentinel wildcard ``"*"`` puts every
# instance into "open mode" — every Odoo model is reachable except the
# hardcoded MODEL_DENYLIST in :mod:`odoo_mcp.security.allowlist`. Users who
# want the old strict behaviour set ``allowed_models = ["res.partner", ...]``
# explicitly in TOML (globally or per-instance).
_DEFAULT_ALLOWED_MODELS: Final[tuple[str, ...]] = ("*",)

# The locale handed to Odoo via the call context. Operator config — never
# caller input — but we still validate the shape so a typo in config.toml
# fails loudly at load time instead of silently producing an untranslated
# session. Odoo ``res.lang`` codes are ISO ``ll`` / ``ll_CC`` with an
# optional ``@variant`` modifier (e.g. ``en_US``, ``nl_BE``, ``sr@latin``).
_DEFAULT_LANGUAGE: Final[str] = "en_US"
_LANGUAGE_RE: Final = re.compile(r"^[a-z]{2,3}(_[A-Z]{2})?(@[A-Za-z]+)?$")

_VALID_DEFAULT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "timeout_seconds",
        "max_records_default",
        "max_records_hard_cap",
        "audit_log",
        "allowed_models",
        "fields_cache_path",
        "rotation_warning_days",
        "language",
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
        "sensitive_fields",
        "refuse_admin_on_production",
        "custom_sensitive_field_patterns",
        "max_commits_per_unlock",
        "smart_fields_overrides",
        "external_comms_enabled",
        "language",
    }
)


@dataclass(frozen=True, slots=True)
class Defaults:
    timeout_seconds: int = 30
    max_records_default: int = 50
    max_records_hard_cap: int = 500
    audit_log: str = DEFAULT_AUDIT_LOG
    allowed_models: tuple[str, ...] = _DEFAULT_ALLOWED_MODELS
    # Empty string disables the persistent fields cache entirely (the L1
    # in-memory cache on OdooClient still applies).
    fields_cache_path: str = DEFAULT_FIELDS_CACHE
    # Doctor emits a warning when an instance's API key was last set more
    # than this many days ago. Odoo doesn't enforce a key TTL; this is the
    # MCP's local reminder. Set lower for stricter regimes; setting to 0
    # effectively warns every run. Recorded set-time comes from the OS
    # credential store via :mod:`odoo_mcp._credstore`.
    rotation_warning_days: int = 90
    # Locale code injected into every Odoo call context. Drives the language
    # of translated field labels, selection-value labels, and translatable
    # record fields. Per-instance ``language`` overrides this default.
    language: str = _DEFAULT_LANGUAGE


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
    sensitive_fields: dict[str, frozenset[str]] = field(default_factory=dict)
    refuse_admin_on_production: bool = True
    custom_sensitive_field_patterns: tuple[str, ...] = ()
    max_commits_per_unlock: int = 10
    # Per-model override of the smart-default field list used by
    # ``odoo_search_read`` / ``odoo_read`` when the caller omits ``fields``.
    # Keys are model strings, values are tuples of field names. When a model
    # is present here the smart-selection heuristic is bypassed entirely
    # for that model and the configured list is returned as-is (sensitive
    # field redaction still applies on the response side).
    smart_fields_overrides: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # External communications (email / log notes via Odoo's message_post,
    # later WhatsApp + SMS) are gated by TWO independent opt-ins: this
    # per-instance flag AND the ``ODOO_MCP_ENABLE_EXTERNAL_COMMS`` env
    # var. Both must be set for ``odoo_send_message`` to be reachable.
    # Default ``False`` — the safe stance is "the MCP cannot email
    # anyone unless the operator explicitly enables it".
    external_comms_enabled: bool = False
    # Odoo locale code (e.g. ``en_US``, ``nl_BE``) injected into the call
    # context so translated labels and selection values come back in the
    # consultant's language. Inherits ``[defaults].language`` when unset.
    language: str = _DEFAULT_LANGUAGE


@dataclass(frozen=True, slots=True)
class AppConfig:
    path: Path
    defaults: Defaults
    instances: dict[str, InstanceConfig] = field(default_factory=dict)
    audit_log_path: Path = field(default_factory=lambda: Path(DEFAULT_AUDIT_LOG).expanduser())
    # ``None`` means the persistent fields cache is disabled (the user set
    # ``fields_cache_path = ""`` in [defaults]). ``Path("")`` would be
    # ambiguous, so we model "off" as an absent path.
    fields_cache_path: Path | None = field(
        default_factory=lambda: Path(DEFAULT_FIELDS_CACHE).expanduser()
    )


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
    fields_cache_path: Path | None
    if defaults.fields_cache_path == "":
        fields_cache_path = None
    else:
        fields_cache_path = Path(defaults.fields_cache_path).expanduser()

    return AppConfig(
        path=cfg_path,
        defaults=defaults,
        instances=instances,
        audit_log_path=audit_log_path,
        fields_cache_path=fields_cache_path,
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
    fields_cache_raw = raw.get("fields_cache_path", DEFAULT_FIELDS_CACHE)
    if not isinstance(fields_cache_raw, str):
        raise ConfigError(
            f'[defaults].fields_cache_path must be a string (use "" to disable), '
            f"got {type(fields_cache_raw).__name__}"
        )
    return Defaults(
        timeout_seconds=_require_int(raw, "timeout_seconds", 30, minimum=1, maximum=120),
        max_records_default=_require_int(raw, "max_records_default", 50, minimum=1, maximum=1000),
        max_records_hard_cap=_require_int(
            raw, "max_records_hard_cap", 500, minimum=1, maximum=10_000
        ),
        audit_log=str(raw.get("audit_log", DEFAULT_AUDIT_LOG)),
        allowed_models=tuple(_require_str_list(raw, "allowed_models", _DEFAULT_ALLOWED_MODELS)),
        fields_cache_path=fields_cache_raw,
        rotation_warning_days=_require_int(
            raw, "rotation_warning_days", 90, minimum=0, maximum=3650
        ),
        language=_require_language(raw, "language", _DEFAULT_LANGUAGE, "defaults"),
    )


def _parse_instances(raw: dict[str, Any], defaults: Defaults) -> dict[str, InstanceConfig]:
    out: dict[str, InstanceConfig] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            raise ConfigError(f"[instances.{name}] must be a table, got {type(entry).__name__}")
        out[name] = _parse_one_instance(name, entry, defaults)

    # Enforce env-prefix uniqueness: two instances sharing a prefix would
    # read each other's credentials from the environment. That's a silent
    # footgun, so fail loudly at config-load time.
    seen: dict[str, str] = {}
    for name, inst in out.items():
        prev = seen.get(inst.credentials_env_prefix)
        if prev is not None:
            raise ConfigError(
                f"Instances {prev!r} and {name!r} share the same "
                f"credentials_env_prefix {inst.credentials_env_prefix!r}. "
                f"Each instance must have its own unique prefix."
            )
        seen[inst.credentials_env_prefix] = name
    return out


def _parse_one_instance(name: str, entry: dict[str, Any], defaults: Defaults) -> InstanceConfig:
    _reject_unknown_keys(entry, _VALID_INSTANCE_KEYS, f"instances.{name}")

    # URL + production coherence.
    url = _require_str(entry, "url", f"instances.{name}")
    if not url.startswith(("https://", "http://")):
        raise ConfigError(
            f"[instances.{name}].url must start with http:// or https:// (got {url!r})"
        )
    production = bool(entry.get("production", False))
    if production and not url.startswith("https://"):
        raise ConfigError(
            f"[instances.{name}] is marked production but url is not HTTPS. Refusing."
        )

    # Credentials: database + env prefix.
    database = _require_str(entry, "database", f"instances.{name}")
    env_prefix = _require_str(entry, "credentials_env_prefix", f"instances.{name}")
    if not env_prefix.replace("_", "").isalnum():
        raise ConfigError(
            f"[instances.{name}].credentials_env_prefix must be alphanumeric + underscore"
        )

    # TLS-strictness policy: no self-signed certs in production.
    allow_self_signed = bool(entry.get("allow_self_signed", False))
    if production and allow_self_signed:
        raise ConfigError(
            f"[instances.{name}] cannot set allow_self_signed on a production instance"
        )

    # Rate limits and record caps.
    default_rate = 60 if production else 300
    rate_limit = _require_int(
        entry, "rate_limit_per_minute", default_rate, minimum=1, maximum=10_000
    )
    timeout = _require_int(
        entry, "timeout_seconds", defaults.timeout_seconds, minimum=1, maximum=120
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

    # Allowlists.
    models = frozenset(_require_str_list(entry, "allowed_models", list(defaults.allowed_models)))
    if not models:
        raise ConfigError(f"[instances.{name}].allowed_models cannot be empty")
    sensitive_fields = _parse_sensitive_fields(entry.get("sensitive_fields"), name)

    refuse_admin_on_production = bool(entry.get("refuse_admin_on_production", True))

    raw_patterns = _require_str_list(entry, "custom_sensitive_field_patterns", [])
    for pattern in raw_patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError(
                f"[instances.{name}].custom_sensitive_field_patterns contains invalid "
                f"regex {pattern!r}: {exc}"
            ) from exc
    custom_patterns = tuple(raw_patterns)

    max_commits = _require_int(entry, "max_commits_per_unlock", 10, minimum=1, maximum=1000)
    smart_overrides = _parse_smart_fields_overrides(entry.get("smart_fields_overrides"), name)
    external_comms = bool(entry.get("external_comms_enabled", False))
    language = _require_language(entry, "language", defaults.language, f"instances.{name}")

    return InstanceConfig(
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
        sensitive_fields=sensitive_fields,
        refuse_admin_on_production=refuse_admin_on_production,
        custom_sensitive_field_patterns=custom_patterns,
        max_commits_per_unlock=max_commits,
        smart_fields_overrides=smart_overrides,
        external_comms_enabled=external_comms,
        language=language,
    )


def _parse_sensitive_fields(raw: Any, instance_name: str) -> dict[str, frozenset[str]]:
    """Parse ``[instances.NAME.sensitive_fields]`` into a model -> frozenset map.

    Keys are model strings, values are lists of field names. An explicit empty
    list means "hide no fields for this model" — it overrides the global default
    to the empty set. Absent models fall back to the hardcoded global default
    in :mod:`odoo_mcp.security.fields`.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(
            f"[instances.{instance_name}.sensitive_fields] must be a table, "
            f"got {type(raw).__name__}"
        )
    out: dict[str, frozenset[str]] = {}
    for model, fields_list in raw.items():
        if not isinstance(model, str) or not model:
            raise ConfigError(
                f"[instances.{instance_name}.sensitive_fields] keys must be non-empty "
                f"model strings, got {model!r}"
            )
        if not isinstance(fields_list, list):
            raise ConfigError(
                f"[instances.{instance_name}.sensitive_fields.{model}] must be a list "
                f"of field names, got {type(fields_list).__name__}"
            )
        for fname in fields_list:
            if not isinstance(fname, str) or not fname:
                raise ConfigError(
                    f"[instances.{instance_name}.sensitive_fields.{model}] entries must "
                    f"be non-empty strings, got {fname!r}"
                )
        out[model] = frozenset(fields_list)
    return out


def _parse_smart_fields_overrides(raw: Any, instance_name: str) -> dict[str, tuple[str, ...]]:
    """Parse ``[instances.NAME.smart_fields_overrides]`` into a model -> tuple map.

    Order matters here (unlike :func:`_parse_sensitive_fields`): the configured
    field list is returned to Claude in the order the consultant wrote it. An
    explicit empty list is rejected — if you don't want a smart default for a
    model, just don't add an override for it.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(
            f"[instances.{instance_name}.smart_fields_overrides] must be a table, "
            f"got {type(raw).__name__}"
        )
    out: dict[str, tuple[str, ...]] = {}
    for model, fields_list in raw.items():
        if not isinstance(model, str) or not model:
            raise ConfigError(
                f"[instances.{instance_name}.smart_fields_overrides] keys must be "
                f"non-empty model strings, got {model!r}"
            )
        if not isinstance(fields_list, list) or not fields_list:
            raise ConfigError(
                f"[instances.{instance_name}.smart_fields_overrides.{model}] must be "
                f"a non-empty list of field names"
            )
        for fname in fields_list:
            if not isinstance(fname, str) or not fname:
                raise ConfigError(
                    f"[instances.{instance_name}.smart_fields_overrides.{model}] "
                    f"entries must be non-empty strings, got {fname!r}"
                )
            if "." in fname:
                raise ConfigError(
                    f"[instances.{instance_name}.smart_fields_overrides.{model}] "
                    f"may not contain dotted field traversals: {fname!r}"
                )
        out[model] = tuple(fields_list)
    return out


def _reject_unknown_keys(raw: dict[str, Any], valid: frozenset[str], section: str) -> None:
    unknown = set(raw.keys()) - valid
    if unknown:
        raise ConfigError(
            f"Unknown keys in [{section}]: {sorted(unknown)}. Valid keys: {sorted(valid)}"
        )


def _require_str(raw: dict[str, Any], key: str, section: str) -> str:
    """Return ``raw[key]`` if present and a string; raise ConfigError otherwise."""
    if key not in raw or not isinstance(raw[key], str):
        raise ConfigError(f"[{section}].{key} is required and must be a string")
    value: str = raw[key]
    return value


def _require_language(raw: dict[str, Any], key: str, default: str, section: str) -> str:
    """Return ``raw[key]`` as a validated Odoo locale code, or ``default``.

    The value goes into the Odoo call context, so a malformed locale would
    otherwise fail silently (Odoo just ignores an unknown ``lang``). We
    validate the shape here so a typo is loud at config-load time.
    """
    value = raw.get(key, default)
    if not isinstance(value, str) or not _LANGUAGE_RE.match(value):
        raise ConfigError(
            f"[{section}].{key} must be an Odoo locale code like 'en_US' or 'nl_BE' (got {value!r})"
        )
    return value


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
