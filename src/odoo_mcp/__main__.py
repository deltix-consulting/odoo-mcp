"""Command-line entry point.

Usage::

    python -m odoo_mcp                          # run the MCP stdio server
    python -m odoo_mcp doctor                   # pre-flight health check
    python -m odoo_mcp status                   # runtime visibility report
    python -m odoo_mcp audit [--tail N]         # inspect the audit log
                  [--errors] [--instance NAME] [--since MINUTES]
    python -m odoo_mcp setup                    # first-time setup wizard
    python -m odoo_mcp setup --add              # add an Odoo instance
    python -m odoo_mcp setup --remove           # remove an Odoo instance
    python -m odoo_mcp setup --list             # list configured instances
    python -m odoo_mcp setup --rotate-key NAME  # rotate API key
    python -m odoo_mcp setup --regenerate-launcher  # rewrite launch.sh
    python -m odoo_mcp config show              # dump the effective config (sanitized)
    python -m odoo_mcp config validate [PATH]   # validate a config file
    python -m odoo_mcp launch                   # load keychain creds + start server (used by launch.sh)
    python -m odoo_mcp launch-env               # print export lines for launch.sh (legacy)
    python -m odoo_mcp uninstall                # remove config, credentials, launcher, registration
    python -m odoo_mcp update                   # self-update from git + uv sync
    python -m odoo_mcp update --check           # check for a newer release only
    python -m odoo_mcp scan-custom INSTANCE     # discover klant-custom models / fields
                  [--toml | --json]
"""

from __future__ import annotations

import asyncio
import sys


def main() -> int:
    # Configure stderr logging before any other import that might log.
    # Off by default; opt in with ODOO_MCP_LOG_LEVEL=DEBUG/INFO/WARNING/ERROR.
    from .logging_setup import configure_logging

    configure_logging()

    argv = sys.argv[1:]

    # ``launch`` is a transparent prefix: it loads credentials from the
    # macOS Keychain into ``os.environ`` and then proceeds as if the
    # ``launch`` token were not there. That lets the launcher script run
    # both ``launch.sh`` (no args -> server) and ``launch.sh doctor``
    # (env-load + doctor) through the same single ``uv run`` invocation.
    if argv and argv[0] == "launch":
        from .setup_wizard import load_launch_env_into_os

        load_launch_env_into_os()
        argv = argv[1:]
        sys.argv = [sys.argv[0], *argv]

    if argv and argv[0] == "doctor":
        from . import doctor

        return doctor.main(argv[1:])

    if argv and argv[0] == "status":
        from . import status_cli

        return status_cli.main(argv[1:])

    if argv and argv[0] == "audit":
        from . import audit_cli

        return audit_cli.main(argv[1:])

    if argv and argv[0] == "setup":
        from . import setup_wizard

        return setup_wizard.main(argv[1:])

    if argv and argv[0] == "config":
        from . import config_cli

        return config_cli.main(argv[1:])

    if argv and argv[0] == "update":
        from . import update_cli

        return update_cli.main(argv[1:])

    if argv and argv[0] == "cache":
        from . import cache_cli

        return cache_cli.main(argv[1:])

    if argv and argv[0] == "scan-custom":
        from . import scan_cli

        return scan_cli.main(argv[1:])

    if argv and argv[0] == "launch-env":
        from .setup_wizard import print_launch_env

        return print_launch_env()

    if argv and argv[0] == "uninstall":
        from . import setup_wizard

        return setup_wizard.uninstall_main(argv[1:])

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
