"""Security-first Odoo MCP server.

Public surface is intentionally small. Import `run` from `odoo_mcp.server` to
launch the stdio server, or use `python -m odoo_mcp` from the command line.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("odoo-mcp")
except PackageNotFoundError:
    __version__ = "dev"

__all__ = ["__version__"]
