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
  ``WARNING``, ``ERROR``) and a :class:`logging.StreamHandler` pointing at
  :data:`sys.stderr` is installed with a compact ``time level module message``
  format. No external deps — stdlib :mod:`logging` only.
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
    "ERROR": logging.ERROR,
}


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

    raw = os.environ.get(_ENV_VAR, "OFF").strip().upper()
    level = _VALID_LEVELS.get(raw)
    if level is None:
        # OFF or any unrecognised value: install a NullHandler and silence.
        logger.addHandler(logging.NullHandler())
        logger.setLevel(logging.CRITICAL + 1)
        return

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    handler.addFilter(_RedactFilter())
    handler.setLevel(level)
    logger.addHandler(handler)
    logger.setLevel(level)
