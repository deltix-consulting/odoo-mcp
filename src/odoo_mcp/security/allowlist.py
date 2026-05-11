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
        # Rights-modification vector: any write to these can grant or
        # revoke privileges. Reading them also leaks per-user permission
        # shape. Full denylist (read + write), not config-overridable.
        "res.users",
        "res.users.log",
        "res.users.apikeys",
        "res.users.apikeys.description",
        "res.users.apikeys.show",  # transient wizard that echoes new key
        "res.users.identitycheck",
        "res.users.deletion",  # pending GDPR-style user deletions
        "res.users.settings",  # holds OAuth refresh tokens via inherit
        "res.users.settings.volumes",
        "res.users.role",  # Enterprise role-based access: assignable rights
        "res.users.role.line",  # role-assignment join (period-scoped roles)
        "res.groups",
        "auth_totp.device",
        # NOTE: auth_oauth.provider and auth_signup.reset.password are kept
        # as legacy entries. The actual Odoo model is `auth.oauth.provider`
        # (with a dot), added below; `auth_signup.reset.password` is not a
        # real model in Odoo 18.0 but the entry is harmless and would block
        # any future module that registered that name.
        "auth_oauth.provider",
        "auth_signup.reset.password",
        "auth.oauth.provider",  # OAuth client_id / endpoints / scopes
        "auth.passkey.key",  # WebAuthn credentials (sign_count, key handles)
        "auth.totp.rate.limit.log",  # 2FA attempt log — auth telemetry
        # System configuration and ACL rules
        "ir.config_parameter",
        "ir.model.access",
        "ir.rule",
        "ir.default",  # default values across any model — write-side sneak
        "ir.filters",  # saved searches with arbitrary domains
        # Stored code / executable content (injection + exec risk)
        # Writing to any of these can run Python or modify what other
        # users see; in either case it's a privilege-escalation path.
        "ir.actions.server",
        "ir.actions.client",
        "ir.actions.act_url",  # URL-redirect actions — phishing vector
        "ir.actions.todo",  # configuration-wizard queue
        "ir.embedded.actions",  # embedded buttons / hidden actions
        "ir.ui.view",
        "ir.asset",  # frontend JS / CSS assets — XSS vector on write
        "mail.template",
        # Automated actions: condition-triggered jobs that can run Python
        # or modify other records under sudo. Same threat class as
        # ir.actions.server; a write here is rights modification by proxy.
        "base.automation",
        "base.automation.lint",
        "base.automation.line.test",
        # Optional companion addon (odoo_addon/odoo_mcp_companion):
        # explicitly controls who can act through the MCP. Even when
        # the addon is installed and exposed, the MCP must never
        # let itself reconfigure its own gate.
        "mcp.access.profile",
        # Mail server credentials and gateway/credential storage
        "ir.mail_server",  # smtp_user / smtp_pass
        "fetchmail.server",  # incoming mail credentials, oauth tokens
        "mail.gateway.allowed",  # mail-routing bypass allowlist
        "google.gmail.mixin",  # google_gmail_refresh_token storage
        "microsoft.outlook.mixin",  # microsoft_outlook_refresh_token storage
        "google.service",
        "microsoft.service",
        "google.calendar.sync",
        "microsoft.calendar.sync",
        # IAP (Odoo's metered API service) account tokens
        "iap.account",  # account_token field
        "iap.service",
        # Payment provider tokens / transactions (PCI scope)
        "payment.token",  # tokenized cards / saved payment methods
        "payment.transaction",
        "payment.provider",
        "payment.method",
        # Scheduler / module / logging internals
        "ir.cron",
        "ir.cron.progress",
        "ir.cron.trigger",
        "ir.module.module",
        "ir.module.category",
        "ir.logging",
        "ir.profile",  # full SQL/Python stack-trace profiles
        "ir.sequence",
        # Real-time bus / presence (not business data, just noise)
        "bus.bus",
        "bus.presence",
        # Model metadata itself (noisy, little business value)
        "ir.model",
        "ir.model.fields",
        "ir.model.fields.selection",
        "ir.model.constraint",
        "ir.model.relation",
        "ir.model.inherit",
        "ir.model.data",
        # Raw attachments — can reference any model
        "ir.attachment",
        # Import/export infrastructure (exfil vector)
        "base_import.import",
        "base_import.mapping",
        "ir.exports",  # saved export specs reusable for exfil
        "ir.exports.line",
    }
)


# Models that are read-allowed but write-forbidden through the MCP. Even
# when production writes are unlocked, ``create`` / ``write`` /
# ``archive`` / ``unlink`` calls against these models are refused. This
# lets us expose ``mail.message`` (chat messages and log notes) and a
# couple of related notification tables as data — Claude can read them
# to answer questions — without giving Claude a side door to actually
# send messages, post log notes, manage followers, or write into the
# notification table on a user's behalf.
#
# Like :data:`MODEL_DENYLIST`, this is a hard safety invariant: it is
# NOT overrideable from config. Adding to it tightens; removing should
# be very rare and reviewed.
MODEL_WRITE_BLOCKLIST: Final[frozenset[str]] = frozenset(
    {
        "mail.message",
        "mail.followers",
        "mail.notification",
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
    DIAGNOSE_ACCESS = "diagnose_access"  # used only by odoo_diagnose_access
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
        Operation.DIAGNOSE_ACCESS,
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
