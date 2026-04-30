"""Model and operation allowlist.

The dispatcher calls :func:`check_model` on every inbound tool call. The set
of allowed models comes from :class:`odoo_mcp.config.InstanceConfig`, so each
instance can override the default if needed.

Two allowlist modes are supported:

* **Open mode** (default since v0.4.0): ``allowed`` contains the sentinel
  ``"*"``. Any Odoo model name passes shape validation and is accepted,
  *except* those listed in :data:`MODEL_DENYLIST` — a hardcoded, non-overrideable
  set of sensitive internal models.
* **Strict mode**: ``allowed`` is a concrete set of model strings; anything
  outside it is rejected. The denylist still applies in strict mode, but in
  practice users building strict lists never include denied models anyway.

The denylist is deliberately not configurable. It is a safety invariant — the
models listed here are either credential tables, ACL rule definitions, stored
executable content (code / templates), or cross-model side-doors. Opting them
back in from a TOML file would defeat the whole point.

Operations are a closed set defined here. Nothing outside it is exposed —
specifically, no ``execute_kw`` for arbitrary methods, no ``copy`` /
``name_search`` / ``fields_view_get``. ``unlink`` IS exposed, but only via the
dedicated ``odoo_archive_or_delete`` tool, which forces an explicit mode
choice and goes through the full prod-guard / dry-run / token flow.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from ..errors import ModelNotAllowedError, OperationNotAllowedError

# Sentinel entry for open-mode allowlists. A ``frozenset`` that contains this
# string means "any model that is not in MODEL_DENYLIST is acceptable".
ALLOWLIST_WILDCARD: Final[str] = "*"

# Models that are ALWAYS blocked, even in open mode. These are either
# - auth / user / group tables (password hashes, session, API keys)
# - ACL / rule definitions themselves (don't let Claude rewrite security)
# - stored code / templates (Python in ir.actions.server, Jinja in
#   mail.template, QWeb in ir.ui.view — both prompt-injection vectors
#   and code-exec vectors)
# - system configuration (ir.config_parameter — often holds external
#   integration secrets)
# - scheduler / module administration
# - raw attachments (can contain arbitrary file content across any
#   res_model). Opt in per-instance if you specifically need it.
MODEL_DENYLIST: Final[frozenset[str]] = frozenset(
    {
        # Auth / user / group
        "res.users",
        "res.users.log",
        "res.users.apikeys",
        "res.users.apikeys.description",
        "res.users.identitycheck",
        "res.groups",
        "auth_totp.device",
        "auth_oauth.provider",
        "auth_signup.reset.password",
        # System configuration and ACL rules
        "ir.config_parameter",
        "ir.model.access",
        "ir.rule",
        # Stored code / executable content (injection + exec risk)
        "ir.actions.server",
        "ir.actions.client",
        "ir.ui.view",
        "mail.template",
        # Scheduler / module / logging internals
        "ir.cron",
        "ir.module.module",
        "ir.logging",
        "ir.sequence",
        # Model metadata itself (noisy, little business value)
        "ir.model",
        "ir.model.fields",
        "ir.model.data",
        # Raw attachments — can reference any model
        "ir.attachment",
        # Import/export infrastructure (exfil vector)
        "base_import.import",
        "base_import.mapping",
    }
)


class Operation(StrEnum):
    """The only operations the MCP is allowed to execute against Odoo."""

    SEARCH_READ = "search_read"
    SEARCH_COUNT = "search_count"
    READ = "read"
    READ_GROUP = "read_group"
    LOOKUP = "lookup"
    CREATE = "create"
    WRITE = "write"
    ARCHIVE = "archive"
    UNLINK = "unlink"
    FIELDS_GET = "fields_get"  # used only by odoo_describe_model
    HELP = "help"  # used only by odoo_help (no Odoo round-trip)
    LIST_INSTANCES = "list_instances"  # used only by odoo_list_instances


_READ_OPS: Final[frozenset[Operation]] = frozenset(
    {
        Operation.SEARCH_READ,
        Operation.SEARCH_COUNT,
        Operation.READ,
        Operation.READ_GROUP,
        Operation.LOOKUP,
        Operation.FIELDS_GET,
        Operation.HELP,
        Operation.LIST_INSTANCES,
    }
)
_WRITE_OPS: Final[frozenset[Operation]] = frozenset(
    {Operation.CREATE, Operation.WRITE, Operation.ARCHIVE, Operation.UNLINK}
)


def is_write(op: Operation) -> bool:
    return op in _WRITE_OPS


def is_read(op: Operation) -> bool:
    return op in _READ_OPS


def check_model(model: str, allowed: frozenset[str]) -> None:
    """Validate ``model`` against the shape check, denylist, and allowlist.

    Pipeline:

    1. Name shape: non-empty string of lowercase-convention identifier
       characters (alnum, ``.``, ``_``).
    2. Denylist: reject anything in :data:`MODEL_DENYLIST`, regardless of
       allowlist mode. This is a safety invariant and cannot be bypassed
       via config.
    3. Allowlist: in open mode (``allowed`` contains the wildcard sentinel
       ``"*"``), anything that survived steps 1–2 passes. In strict mode,
       ``model`` must be an exact member of ``allowed``.

    Matching is exact and case-sensitive. Odoo model names are lowercase by
    convention and dotted (e.g. ``res.partner``), so any deviation is almost
    certainly a mistake and should fail loudly.
    """
    if not isinstance(model, str) or not model:
        raise ModelNotAllowedError("Model name must be a non-empty string.")
    # Reject anything that looks like an injection attempt — model names are
    # lowercase, dotted identifiers. Don't accept slashes, quotes, whitespace.
    for ch in model:
        if not (ch.isalnum() or ch in "._"):
            raise ModelNotAllowedError(f"Model name {model!r} contains invalid characters.")
    if model in MODEL_DENYLIST:
        raise ModelNotAllowedError(
            f"Model {model!r} is blocked by the built-in denylist for security reasons "
            f"(auth tables, ACL rules, stored code, system configuration, or raw "
            f"attachments). This cannot be overridden by config."
        )
    if ALLOWLIST_WILDCARD in allowed:
        # Open mode: anything that survived shape + denylist is fine.
        return
    if model not in allowed:
        raise ModelNotAllowedError(
            f"Model {model!r} is not on the allowlist for this instance. "
            f"Allowed models: {sorted(allowed)}"
        )


def check_operation(op: Operation | str) -> Operation:
    """Coerce ``op`` to a validated :class:`Operation` or raise."""
    if isinstance(op, Operation):
        return op
    try:
        return Operation(op)
    except ValueError as exc:
        raise OperationNotAllowedError(
            f"Operation {op!r} is not exposed by this MCP. Allowed: {[o.value for o in Operation]}"
        ) from exc
