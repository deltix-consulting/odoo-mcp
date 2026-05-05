"""Cross-platform credential storage.

Thin wrapper over :mod:`keyring`. All odoo-mcp Keychain / Credential Manager /
libsecret access goes through these three functions so the rest of the codebase
never has to think about platform branches.

Service-name convention::

    odoo-mcp/{instance}/{service}

That groups all keys for one instance under a common prefix in the OS
credential store, which makes the entries easy to spot in Keychain Access /
Credential Manager / Seahorse.

The username field passed to keyring is the literal *service* string (e.g.
``ODOO_MCP_MAIN_API_KEY``). keyring requires both ``service_name`` and
``username``; we use the service path for grouping and the original service
string for the secondary identifier so two distinct secrets per instance
remain addressable.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime

import keyring
from keyring.errors import PasswordDeleteError

# Suffix used to store the "last set" timestamp for a (instance, service)
# pair in a parallel keyring entry under ``odoo-mcp/{instance}/_meta``.
# Odoo doesn't enforce a TTL on API keys; this lets ``odoo-mcp doctor``
# warn when a key is overdue for rotation. See v0.13.1 F3.
_META_SERVICE: str = "_meta"
_SET_AT_SUFFIX: str = "_set_at"

# Service prefix that, when present in ``service``, marks the secret as
# tracking-worthy (we record set-times for API keys, not for usernames).
_TRACKED_SUFFIXES: tuple[str, ...] = ("_API_KEY",)


def _service_name(instance: str) -> str:
    """Build the OS-store service identifier for an instance."""
    return f"odoo-mcp/{instance}"


def _meta_service_name(instance: str) -> str:
    """The keyring service used to store rotation metadata for an instance."""
    return f"odoo-mcp/{instance}/{_META_SERVICE}"


def _is_tracked(service: str) -> bool:
    return any(service.endswith(suffix) for suffix in _TRACKED_SUFFIXES)


def set_secret(instance: str, service: str, value: str) -> None:
    """Store *value* under (instance, service) in the OS credential store.

    For tracked secrets (currently anything ending in ``_API_KEY``) we
    also write a sibling entry recording the ISO-8601 UTC timestamp at
    which the secret was last set. ``odoo-mcp doctor`` reads that
    timestamp and warns when the key is older than the configured
    rotation threshold (default 90 days). Failure to write the
    timestamp does not abort the secret write — rotation tracking is
    best-effort.
    """
    keyring.set_password(_service_name(instance), service, value)
    if _is_tracked(service):
        ts = datetime.now(UTC).isoformat()
        with contextlib.suppress(Exception):
            keyring.set_password(_meta_service_name(instance), f"{service}{_SET_AT_SUFFIX}", ts)


def get_secret(instance: str, service: str) -> str | None:
    """Read the value for (instance, service); ``None`` if not present."""
    return keyring.get_password(_service_name(instance), service)


def get_secret_set_at(instance: str, service: str) -> datetime | None:
    """Return the UTC datetime at which *service* was last set, or ``None``.

    Returns ``None`` when no timestamp has been recorded (older keys
    set before v0.13.1 won't have one) or when the stored value can't
    be parsed.
    """
    raw: str | None
    try:
        raw = keyring.get_password(_meta_service_name(instance), f"{service}{_SET_AT_SUFFIX}")
    except Exception:  # noqa: BLE001 — best-effort metadata read
        return None
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def delete_secret(instance: str, service: str) -> None:
    """Delete the entry for (instance, service). Silently ignore 'not found'.

    Also drops the rotation-metadata sibling entry (best-effort) so
    deleting and re-creating an instance gives a fresh timestamp rather
    than inheriting the previous one.
    """
    # Match the original ``security delete-generic-password`` behaviour:
    # absent entries are not an error.
    with contextlib.suppress(PasswordDeleteError):
        keyring.delete_password(_service_name(instance), service)
    if _is_tracked(service):
        with contextlib.suppress(PasswordDeleteError, Exception):
            keyring.delete_password(_meta_service_name(instance), f"{service}{_SET_AT_SUFFIX}")
