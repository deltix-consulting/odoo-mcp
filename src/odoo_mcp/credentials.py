"""Credential loading and lifetime management.

Credentials live for the life of the process inside a single :class:`Credentials`
dataclass. They are loaded from environment variables at startup and **deleted
from ``os.environ`` immediately afterwards** so that any child process the MCP
might ever spawn (there shouldn't be any, but defense in depth) cannot inherit
them. The ``__repr__`` / ``__str__`` of the dataclass never return the secret.

Python strings are immutable and we can't genuinely zero them, so the structural
guarantees here are:

1. **Single owner** — the value lives on exactly one dataclass instance.
2. **No f-string interpolation** — we never embed the secret into a larger string
   that then escapes into a log line or error message.
3. **Redaction registry** — the raw value is registered with :mod:`errors` so
   any accidental string-ification anywhere else in the process is scrubbed
   on its way out.
4. **Delete from environment** — ``del os.environ[name]`` (not just set to ``""``)
   so future ``os.execvpe``/``subprocess`` calls don't inherit it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Final

from .errors import CredentialsError, register_secret

_USERNAME_SUFFIX: Final[str] = "_USERNAME"
_API_KEY_SUFFIX: Final[str] = "_API_KEY"


@dataclass(frozen=True, slots=True)
class Credentials:
    """Credentials for one Odoo instance.

    ``api_key`` is held on the instance and never returned through ``__repr__``
    or ``__str__``. Code that needs the raw value should call
    :meth:`reveal_for_rpc`, which exists solely so that grepping for
    ``.api_key`` in this codebase surfaces every place that touches it.
    """

    instance_name: str
    username: str
    _api_key: str = field(repr=False)

    def __str__(self) -> str:
        return f"<credentials instance={self.instance_name} user={self.username} api_key=<redacted>>"

    def __repr__(self) -> str:
        return self.__str__()

    def reveal_for_rpc(self) -> str:
        """Return the raw API key.

        This is the only sanctioned way to get the key. Grep for it.
        """
        return self._api_key


def load_credentials(instance_name: str, env_prefix: str) -> Credentials:
    """Load credentials for one instance from the process environment.

    Reads ``{env_prefix}_USERNAME`` and ``{env_prefix}_API_KEY`` from
    ``os.environ``, constructs a :class:`Credentials` for the instance, and
    then **deletes** those keys from the environment. Registers the secret
    with the error-redaction machinery so it never leaks through an exception.

    Raises :class:`CredentialsError` if either variable is missing or empty.
    """
    username_key = env_prefix + _USERNAME_SUFFIX
    api_key_key = env_prefix + _API_KEY_SUFFIX

    username = os.environ.get(username_key, "").strip()
    api_key = os.environ.get(api_key_key, "")

    missing: list[str] = []
    if not username:
        missing.append(username_key)
    if not api_key:
        missing.append(api_key_key)
    if missing:
        raise CredentialsError(
            f"Missing required environment variables for instance {instance_name!r}: "
            + ", ".join(missing)
        )

    # Register for redaction BEFORE deleting from environ, so any exception
    # raised between here and the delete is still scrubbed.
    register_secret(api_key)

    creds = Credentials(instance_name=instance_name, username=username, _api_key=api_key)

    # Delete (not overwrite) so a subsequent os.environ.get returns None.
    # This prevents any accidental child-process inheritance.
    for key in (username_key, api_key_key):
        if key in os.environ:
            del os.environ[key]

    return creds
