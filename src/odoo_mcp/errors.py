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

import re
from collections import OrderedDict
from typing import ClassVar, Final

# Populated by credentials.register_secret(). We keep it module-level so that
# *any* error, including third-party ones re-raised as OdooMcpError, is scrubbed
# without the caller having to thread a registry through.
#
# The registry is bounded (LRU-evicted) at ``_SECRETS_MAX`` entries — enough
# for any realistic multi-instance setup (one username + one key per instance,
# plus headroom for rotations) without growing without bound across long-lived
# processes. Substring scans were O(n * k) per error; we now compile the
# alternation regex lazily and cache it, recompiling only when the registry
# changes.
_SECRETS_MAX: Final[int] = 64
_SECRETS: OrderedDict[str, None] = OrderedDict()
_SECRETS_PATTERN: re.Pattern[str] | None = None

_REDACTED: Final[str] = "<redacted>"


def register_secret(secret: str) -> None:
    """Register a secret string for automatic redaction in error messages.

    Called by :mod:`odoo_mcp.credentials` right after loading an API key.
    Ignores empty strings (so a misconfigured instance can't accidentally
    register ``""`` and then "redact" every space in every error).

    The registry is LRU-bounded at :data:`_SECRETS_MAX`; when full, the
    oldest entry is evicted before inserting the new one. The compiled
    redaction pattern is invalidated so the next :func:`redact` call
    rebuilds it.
    """
    global _SECRETS_PATTERN
    if not secret or len(secret) < 4:
        return
    if secret in _SECRETS:
        # Refresh recency without changing membership; pattern is unchanged.
        _SECRETS.move_to_end(secret)
        return
    if len(_SECRETS) >= _SECRETS_MAX:
        _SECRETS.popitem(last=False)
    _SECRETS[secret] = None
    _SECRETS_PATTERN = None


def redact(text: str) -> str:
    """Return ``text`` with every registered secret substituted by ``<redacted>``.

    Uses a single compiled regex alternation across all registered secrets,
    so substitution is one pass over the input regardless of registry size.
    The pattern is rebuilt lazily on first use after a registry change.
    """
    global _SECRETS_PATTERN
    if not _SECRETS:
        return text
    pattern = _SECRETS_PATTERN
    if pattern is None:
        # Sort by length descending so a longer secret that contains a
        # shorter one is matched first (e.g. "abcd" before "abc"). Without
        # this, a partial-overlap secret could swallow the longer match.
        ordered = sorted(_SECRETS.keys(), key=len, reverse=True)
        pattern = re.compile("|".join(re.escape(s) for s in ordered))
        _SECRETS_PATTERN = pattern
    return pattern.sub(_REDACTED, text)


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
        # Deliberate behavioural instruction: the user explicitly named an
        # instance that does not exist here, which is a real ambiguity. An
        # AI that "helpfully" re-issues the call against a different real
        # instance (e.g. falling back to prod when the user asked for
        # demo) can read sensitive data or — on an unlocked instance —
        # trigger a dry-run preview against the wrong dataset. Tell the
        # AI to surface the ambiguity back to the user rather than
        # substitute.
        return (
            "STOP — do not silently retry this call against a different "
            "instance. The user named an instance that does not exist on "
            "this machine; ask the user which instance they meant. Do "
            "NOT substitute another real instance (especially production) "
            "based on similarity or guess."
        )


class ModelNotAllowedError(OdooMcpError):
    """Tool call referenced a model that is not on the allowlist."""

    code = "model_not_allowed"

    @property
    def hint(self) -> str:
        return "Contact your MCP administrator if this model should be available."


class OperationNotAllowedError(OdooMcpError):
    """Tool call requested an operation that is not exposed at all."""

    code = "operation_not_allowed"

    @property
    def hint(self) -> str:
        return (
            "This MCP only supports: search_read, search_count, read, read_group, "
            "lookup, create, write, archive, unlink, fields_get."
        )


class ProdGuardError(OdooMcpError):
    """Blocked by the production guard (writes not unlocked, etc.)."""

    code = "prod_guard"

    @property
    def hint(self) -> str:
        return "Production writes require explicit unlock by the operator."


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
