"""Security layer for the Odoo MCP.

Each module here is a small pure function / class with no XML-RPC dependency,
so it can be unit-tested in isolation. The dispatcher in
:mod:`odoo_mcp.server` wires them together in a fixed order and never skips a
step.
"""

from __future__ import annotations
