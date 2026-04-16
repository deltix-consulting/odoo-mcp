"""Command-line entry point.

Usage::

    python -m odoo_mcp            # run the MCP stdio server
    python -m odoo_mcp doctor     # pre-flight health check
"""

from __future__ import annotations

import asyncio
import sys


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] == "doctor":
        from . import doctor

        return doctor.main(argv[1:])

    if argv and argv[0] in {"-h", "--help"}:
        print(__doc__)
        return 0

    from . import server

    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
