"""Security-first Odoo MCP server.

Public surface is intentionally small. Import `run` from `odoo_mcp.server` to
launch the stdio server, or use `python -m odoo_mcp` from the command line.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
