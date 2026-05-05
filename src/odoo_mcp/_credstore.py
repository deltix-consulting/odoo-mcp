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

import keyring
from keyring.errors import PasswordDeleteError


def _service_name(instance: str) -> str:
    """Build the OS-store service identifier for an instance."""
    return f"odoo-mcp/{instance}"


def set_secret(instance: str, service: str, value: str) -> None:
    """Store *value* under (instance, service) in the OS credential store."""
    keyring.set_password(_service_name(instance), service, value)


def get_secret(instance: str, service: str) -> str | None:
    """Read the value for (instance, service); ``None`` if not present."""
    return keyring.get_password(_service_name(instance), service)


def delete_secret(instance: str, service: str) -> None:
    """Delete the entry for (instance, service). Silently ignore 'not found'."""
    try:
        keyring.delete_password(_service_name(instance), service)
    except PasswordDeleteError:
        # Match the original ``security delete-generic-password`` behaviour:
        # absent entries are not an error.
        return
