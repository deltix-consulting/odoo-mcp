"""Optional stderr logging for the Odoo MCP server.

This module exists because the MCP protocol owns stdout in stdio mode, which
means ordinary ``print()``-style debugging is invisible to the user. When
something breaks inside Claude Desktop / Cowork there is no visible trail.

The design is intentionally minimal:

* **Off by default.** If ``ODOO_MCP_LOG_LEVEL`` is unset or ``OFF``, we install
  a :class:`logging.NullHandler` on the ``odoo_mcp`` root logger so that any
  library log call is silently discarded — we never want log noise leaking
  into the stdio channel that carries MCP protocol traffic.
* **Opt in via env var.** Set ``ODOO_MCP_LOG_LEVEL=DEBUG`` (or ``INFO``,
  ``WARNING``, ``ERROR``, ``CRITICAL``) and a :class:`logging.StreamHandler`
  pointing at :data:`sys.stderr` is installed with a compact ``time level
  module message`` format. No external deps — stdlib :mod:`logging` only.
* **A typo is not OFF.** Setting the variable at all is the opt-in; an
  unrecognised value (``DEBGU``, ``TRACE``, ``VERBOSE``) installs the stderr
  handler at ``WARNING`` and prints one line naming the bad value. The
  operator reached for this variable because something is already broken —
  answering a typo with silence is the one outcome that helps nobody. Only
  an unset variable or an explicit ``OFF`` stays silent.
* **Credential-safe.** A filter routes every formatted record through
  :func:`odoo_mcp.errors.redact` so registered secrets never appear in log
  output, even if a third-party library echoes one back.

Call :func:`configure_logging` exactly once, as early as possible in the
process lifetime (i.e. at the top of ``main()`` in ``__main__.py``). Repeat
calls are safe and idempotent — they replace handlers rather than stacking
them.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Final

from .errors import redact

_LOGGER_NAME: Final[str] = "odoo_mcp"
_ENV_VAR: Final[str] = "ODOO_MCP_LOG_LEVEL"
_FORMAT: Final[str] = "%(asctime)s %(levelname)s %(name)s %(message)s"
_DATEFMT: Final[str] = "%Y-%m-%dT%H:%M:%S"

_VALID_LEVELS: Final[dict[str, int]] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,  # stdlib alias; the spelling most other tools use
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}
_OFF: Final[str] = "OFF"
# Level used when the variable is set to something we do not recognise. The
# most conservative level that still shows the failures the operator is
# almost certainly looking for.
_FALLBACK_LEVEL: Final[int] = logging.WARNING


class _RedactFilter(logging.Filter):
    """Scrub registered secrets from the fully-formatted log message.

    The filter runs after the record's ``args`` have been merged with its
    ``msg`` (via :meth:`logging.LogRecord.getMessage`), so it catches secrets
    no matter how they entered the log call — format args, f-strings, or
    bare message strings. The scrubbed text is assigned to ``msg`` with
    ``args`` cleared so handlers don't try to re-merge.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            merged = record.getMessage()
        except Exception:  # noqa: BLE001 — never let logging itself blow up
            return True
        scrubbed = redact(merged)
        if scrubbed != merged or record.args:
            record.msg = scrubbed
            record.args = None
        return True


def configure_logging() -> None:
    """Configure the ``odoo_mcp`` logger from ``ODOO_MCP_LOG_LEVEL``.

    Idempotent: existing handlers on the ``odoo_mcp`` logger are removed
    first so repeat calls don't stack output.
    """
    logger = logging.getLogger(_LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    # Don't bubble up to the root logger — we manage our own output surface.
    logger.propagate = False

    raw = os.environ.get(_ENV_VAR, _OFF).strip().upper()
    if raw in ("", _OFF):
        # Unset or explicitly OFF: install a NullHandler and silence.
        logger.addHandler(logging.NullHandler())
        logger.setLevel(logging.CRITICAL + 1)
        return

    unrecognised = raw not in _VALID_LEVELS
    # The variable IS set, so the operator wants output. Silencing a typo
    # would read a broken value as a clean OFF — fall back to WARNING and
    # say so on the surface they just switched on.
    level = _FALLBACK_LEVEL if unrecognised else _VALID_LEVELS[raw]

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    handler.addFilter(_RedactFilter())
    handler.setLevel(level)
    logger.addHandler(handler)
    logger.setLevel(level)

    if unrecognised:
        logger.warning(
            "%s=%r is not a recognised level (expected one of %s, or OFF); "
            "logging at WARNING instead.",
            _ENV_VAR,
            raw,
            "/".join(k for k in _VALID_LEVELS if k != "WARN"),
        )
