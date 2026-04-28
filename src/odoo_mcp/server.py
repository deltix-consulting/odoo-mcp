"""MCP server entry point.

This module just wires together:

- :mod:`odoo_mcp.tools` — tool schema constants
- :mod:`odoo_mcp.dispatcher` — the security dispatcher
- the MCP stdio loop (``run``)

See :mod:`odoo_mcp.dispatcher` for the per-call security pipeline.
"""

from __future__ import annotations

from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .audit import AuditLog
from .client import OdooClient
from .config import load_config
from .credentials import make_credential_loader
from .dispatcher import (
    Dispatcher,
    InstanceRuntime,
    OdooMcpApp,
    _args_shape,
    _sanitize_details,
)
from .fields_cache import PersistentFieldsCache
from .security.fields import compile_extra_patterns
from .security.limits import RateLimiter
from .security.prod_guard import ProdGuard
from .tools import build_tools

__all__ = [
    "Dispatcher",
    "InstanceRuntime",
    "OdooMcpApp",
    "_args_shape",
    "_sanitize_details",
    "build_app",
    "build_server",
    "run",
]


def build_app(config_path: Any = None) -> OdooMcpApp:
    """Load config, audit log, and per-instance clients.

    This is the single startup function. Config loading is local and still
    fails fast. Credential loading is deferred — each :class:`OdooClient` is
    given a lazy :func:`make_credential_loader` closure that reads env vars
    (and deletes them from ``os.environ``) only on the instance's first use.
    A broken credential config for one instance therefore fails only when
    that instance is actually called, not at process startup. Odoo
    authentication remains deferred to the first tool call per instance.
    """
    cfg = load_config(config_path)
    audit = AuditLog(cfg.audit_log_path)
    prod_guard = ProdGuard()
    rate_limiter = RateLimiter()

    # Build the L2 fields cache once and share across all clients. Disabled
    # if the operator set ``fields_cache_path = ""`` in [defaults].
    fields_cache: PersistentFieldsCache | None = None
    if cfg.fields_cache_path is not None:
        fields_cache = PersistentFieldsCache(cfg.fields_cache_path)

    instances: dict[str, InstanceRuntime] = {}
    for name, inst_cfg in cfg.instances.items():
        loader = make_credential_loader(name, inst_cfg.credentials_env_prefix)
        client = OdooClient(inst_cfg, credential_loader=loader, fields_cache=fields_cache)
        rate_limiter.configure(name, inst_cfg.rate_limit_per_minute)
        extra = compile_extra_patterns(list(inst_cfg.custom_sensitive_field_patterns))
        instances[name] = InstanceRuntime(config=inst_cfg, client=client, extra_redacted=extra)

    return OdooMcpApp(
        config=cfg,
        audit=audit,
        prod_guard=prod_guard,
        rate_limiter=rate_limiter,
        instances=instances,
    )


def build_server(app: OdooMcpApp) -> Server:
    server: Server = Server("odoo-mcp")
    dispatcher = Dispatcher(app)
    tools = build_tools()

    # The mcp SDK's decorators aren't typed; mypy --strict flags the
    # resulting wrapped function. We accept that at this single boundary
    # point.
    @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
    async def _list_tools() -> list[Tool]:
        return tools

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def _call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
        return await dispatcher.call(name, arguments or {})

    return server


async def run() -> None:
    """Entry point for ``python -m odoo_mcp`` (server mode)."""
    app = build_app()
    server = build_server(app)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )
