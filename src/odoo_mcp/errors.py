"""Typed errors for the Odoo MCP server.

Every error is derived from :class:`OdooMcpError`, which provides:

* A stable error **code** for audit logging and for the MCP client to branch on.
* A **user message** that is safe to surface to Claude/the model.
* A redaction hook: the string form of any error, and of any chained cause, is
  scrubbed of credential values before being returned.

The redaction is intentionally defensive. The registry is populated at startup
by :mod:`odoo_mcp.credentials` and contains the raw secret strings; this module
then substitutes them out of any stringified error. This guards against a
third-party library (for example :mod:`xmlrpc.client`) echoing the value back
inside an exception message.
"""

from __future__ import annotations

from typing import ClassVar, Final

# Populated by credentials.register_secret(). We keep it module-level so that
# *any* error, including third-party ones re-raised as OdooMcpError, is scrubbed
# without the caller having to thread a registry through.
_SECRETS: set[str] = set()

_REDACTED: Final[str] = "<redacted>"


def register_secret(secret: str) -> None:
    """Register a secret string for automatic redaction in error messages.

    Called by :mod:`odoo_mcp.credentials` right after loading an API key.
    Ignores empty strings (so a misconfigured instance can't accidentally
    register ``""`` and then "redact" every space in every error).
    """
    if secret and len(secret) >= 4:
        _SECRETS.add(secret)


def redact(text: str) -> str:
    """Return ``text`` with every registered secret substituted by ``<redacted>``.

    This is intentionally O(n * k) over the secrets set; the set is small
    (one per instance) and this path runs only on the error path, not the
    happy path.
    """
    out = text
    for secret in _SECRETS:
        if secret in out:
            out = out.replace(secret, _REDACTED)
    return out


class OdooMcpError(Exception):
    """Base class for every error raised by this package.

    Subclasses set the ``code`` class variable. The ``__str__`` of any instance
    is automatically redaction-scrubbed, and so is the chained cause — so even
    if an underlying library (:mod:`xmlrpc.client`, :mod:`ssl`, etc.) includes
    the secret in its own exception text, it never leaks through our layer.
    """

    code: ClassVar[str] = "odoo_mcp_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self._message = message

    def __str__(self) -> str:
        return redact(self._message)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.__str__()!r})"

    @property
    def user_message(self) -> str:
        """The safe-to-return message shown to the MCP client."""
        return self.__str__()

    @property
    def hint(self) -> str | None:
        """Optional actionable hint for the MCP client. Override in subclasses."""
        return None

    @property
    def cause_message(self) -> str | None:
        """Redaction-scrubbed string form of :attr:`__cause__`, if any."""
        cause = self.__cause__
        if cause is None:
            return None
        return redact(str(cause))


# --- Configuration / startup --------------------------------------------------


class ConfigError(OdooMcpError):
    """The config file is missing, unreadable, malformed, or has weak perms."""

    code = "config_error"


class CredentialsError(OdooMcpError):
    """Credentials env vars are missing or look malformed."""

    code = "credentials_error"


class AuditLogError(OdooMcpError):
    """The audit log is not writable — the server must refuse to run."""

    code = "audit_log_error"


# --- Request-time / security dispatcher --------------------------------------


class InstanceNotFoundError(OdooMcpError):
    """Tool call referenced an instance that is not configured."""

    code = "instance_not_found"

    @property
    def hint(self) -> str:
        return "Use odoo_list_instances to see configured instances."


class ModelNotAllowedError(OdooMcpError):
    """Tool call referenced a model that is not on the allowlist."""

    code = "model_not_allowed"

    @property
    def hint(self) -> str:
        return "Use odoo_list_instances to see which models are available, or ask your administrator to add this model to the config."


class OperationNotAllowedError(OdooMcpError):
    """Tool call requested an operation that is not exposed at all."""

    code = "operation_not_allowed"

    @property
    def hint(self) -> str:
        return "This MCP only supports: search_read, search_count, read, read_group, create, write."


class ProdGuardError(OdooMcpError):
    """Blocked by the production guard (writes not unlocked, etc.)."""

    code = "prod_guard"

    @property
    def hint(self) -> str:
        return "Call odoo_enable_prod_writes first to unlock writes for 15 minutes."


class DomainSandboxError(OdooMcpError):
    """Domain filter rejected by the sandbox."""

    code = "domain_sandbox"


class FieldPolicyError(OdooMcpError):
    """A requested field is redacted or not resolvable."""

    code = "field_policy"


class LimitExceededError(OdooMcpError):
    """A cap or rate limit was exceeded."""

    code = "limit_exceeded"


class OdooTransportError(OdooMcpError):
    """Something went wrong talking to Odoo (network, TLS, timeout, HTTP error).

    Wraps the underlying cause with the cause's string also scrubbed. Callers
    should prefer ``error.user_message`` over ``str(error.__cause__)``.
    """

    code = "odoo_transport"

    @property
    def hint(self) -> str:
        return "Check that the Odoo URL is reachable. Run 'odoo-mcp doctor' to diagnose."


class OdooAuthError(OdooMcpError):
    """Odoo rejected the credentials at ``authenticate`` time."""

    code = "odoo_auth"

    @property
    def hint(self) -> str:
        return "Check your API key and username. Run 'odoo-mcp doctor' to diagnose."


class OdooRemoteError(OdooMcpError):
    """Odoo returned a fault (validation error, access rights, etc.)."""

    code = "odoo_remote"
