"""Print MCP client config snippets so consultants can wire odoo-mcp into
whichever IDE / chat client they use.

The MCP wire protocol is the same everywhere — it's just stdio with JSON-RPC
— but each client has its own config file and JSON shape. This CLI emits a
ready-to-paste snippet for the popular clients.

Why a CLI command rather than just docs? Two reasons:

1. The ``command`` field needs the absolute path to the ``odoo-mcp``
   executable on this machine. That path varies (``~/.local/bin/odoo-mcp``,
   ``%USERPROFILE%\\.local\\bin\\odoo-mcp.exe``, etc.) and copy-pasting the
   wrong one is the #1 onboarding footgun. Resolving via ``shutil.which``
   here removes the guesswork.

2. Some clients (Cursor, Windsurf, VS Code-Continue, Codex) accept slightly
   different keys. Printing per-client snippets sidesteps "but I have to
   translate the Claude Desktop example" friction.

This command is informational only — it never writes any client config
files itself. ``odoo-mcp setup`` registers Claude Desktop and Codex; for
everything else the user copies the snippet manually so it lands in the
right config file with the right surrounding structure.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Final


def _resolve_command() -> str:
    found = shutil.which("odoo-mcp")
    return found or "odoo-mcp"


_SUPPORTED_CLIENTS: Final[list[tuple[str, str]]] = [
    ("claude-desktop", "Claude Desktop (claude_desktop_config.json)"),
    ("claude-code", "Claude Code CLI (~/.claude.json)"),
    ("codex", "OpenAI Codex CLI (~/.codex/config.toml)"),
    ("cursor", "Cursor IDE (~/.cursor/mcp.json)"),
    ("windsurf", "Windsurf / Codeium IDE"),
    ("continue", "Continue.dev for VS Code / JetBrains"),
    ("zed", "Zed editor (~/.config/zed/settings.json)"),
    ("generic-stdio", "Any MCP-compliant stdio client"),
]


def _block_for(client: str, command: str) -> str:
    """Return the config snippet for ``client`` as a printable string."""
    body: dict[str, Any]
    if client == "claude-desktop":
        body = {
            "mcpServers": {
                "odoo-mcp": {"command": command, "args": ["launch"]},
            }
        }
        return _json_block(
            body,
            footer=(
                "Path: ~/Library/Application Support/Claude/claude_desktop_config.json (macOS)\n"
                "      %APPDATA%\\Claude\\claude_desktop_config.json (Windows)\n"
                "      ~/.config/Claude/claude_desktop_config.json (Linux)"
            ),
        )

    if client == "claude-code":
        body = {
            "mcpServers": {
                "odoo-mcp": {"command": command, "args": ["launch"]},
            }
        }
        return _json_block(
            body,
            footer=(
                "Path: ~/.claude.json (claude code's per-project file).\n"
                "Or run: claude mcp add odoo-mcp "
                f"-- {command} launch"
            ),
        )

    if client == "codex":
        # Codex uses TOML, not JSON.
        toml = f'[mcp_servers.odoo-mcp]\ncommand = "{command}"\nargs = ["launch"]\n'
        return f"{toml}\nPath: ~/.codex/config.toml"

    if client == "cursor":
        body = {
            "mcpServers": {
                "odoo-mcp": {"command": command, "args": ["launch"]},
            }
        }
        return _json_block(
            body,
            footer=("Path: ~/.cursor/mcp.json (global) or .cursor/mcp.json (per-project)"),
        )

    if client == "windsurf":
        body = {
            "mcpServers": {
                "odoo-mcp": {"command": command, "args": ["launch"]},
            }
        }
        return _json_block(
            body,
            footer="Path: ~/.codeium/windsurf/mcp_config.json",
        )

    if client == "continue":
        body = {
            "experimental": {
                "modelContextProtocolServers": [
                    {
                        "transport": {
                            "type": "stdio",
                            "command": command,
                            "args": ["launch"],
                        }
                    }
                ]
            }
        }
        return _json_block(
            body,
            footer=(
                "Path: ~/.continue/config.json (Continue's MCP support is "
                "still flagged experimental — track their docs for the latest key.)"
            ),
        )

    if client == "zed":
        body = {
            "context_servers": {
                "odoo-mcp": {
                    "command": {
                        "path": command,
                        "args": ["launch"],
                    }
                }
            }
        }
        return _json_block(body, footer="Path: ~/.config/zed/settings.json")

    if client == "generic-stdio":
        return (
            "Any MCP-compliant client can launch the server over stdio with:\n\n"
            f"  {command} launch\n\n"
            "Environment is loaded from the OS credential store (Keychain / "
            "Credential Manager / libsecret), so no env vars need to be set "
            "by the parent process.\n"
        )

    raise ValueError(f"Unknown client: {client!r}")


def _json_block(body: object, *, footer: str) -> str:
    rendered = json.dumps(body, indent=2)
    return f"{rendered}\n\n{footer}"


def _supported_set() -> set[str]:
    return {name for name, _desc in _SUPPORTED_CLIENTS}


def _detect_installed() -> list[str]:
    """Best-effort detection of which clients are installed locally.

    Used by ``--detect``. Pure file-existence checks; never reads the
    contents of any client's config. Returns the set of client names whose
    expected config dir / file is present.
    """
    home = Path.home()
    candidates: list[tuple[str, list[Path]]] = [
        (
            "claude-desktop",
            [
                home / "Library/Application Support/Claude/claude_desktop_config.json",
                home / "AppData/Roaming/Claude/claude_desktop_config.json",
                home / ".config/Claude/claude_desktop_config.json",
            ],
        ),
        ("claude-code", [home / ".claude.json"]),
        ("codex", [home / ".codex/config.toml"]),
        ("cursor", [home / ".cursor"]),
        ("windsurf", [home / ".codeium/windsurf"]),
        ("continue", [home / ".continue"]),
        ("zed", [home / ".config/zed/settings.json"]),
    ]
    found: list[str] = []
    for name, paths in candidates:
        if any(p.exists() for p in paths):
            found.append(name)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="odoo-mcp client-config",
        description=(
            "Print a ready-to-paste config snippet for an MCP client. "
            "Pick one with --client, list with --list, or use --detect "
            "to print snippets for clients found on this machine."
        ),
    )
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument(
        "--client",
        choices=sorted(_supported_set()),
        help="Print the snippet for one specific client.",
    )
    group.add_argument(
        "--list",
        action="store_true",
        help="List supported clients and exit.",
    )
    group.add_argument(
        "--detect",
        action="store_true",
        help="Print snippets for every client whose config dir is present locally.",
    )
    ns = parser.parse_args(argv)

    if ns.list:
        for name, desc in _SUPPORTED_CLIENTS:
            print(f"  {name:<16}  {desc}")
        return 0

    command = _resolve_command()
    if command == "odoo-mcp":
        print(
            "# Note: 'odoo-mcp' was not found on PATH. Snippets below use the "
            "bare name; ensure your IDE inherits a PATH that includes "
            "~/.local/bin (POSIX) or %USERPROFILE%\\.local\\bin (Windows), or "
            "edit the snippet to use an absolute path.",
            file=sys.stderr,
        )

    if ns.detect:
        installed = _detect_installed()
        if not installed:
            print(
                "No MCP-compatible client config dir found in the usual "
                "locations. Re-run with --client <name> to see a specific "
                "snippet anyway.",
                file=sys.stderr,
            )
            return 1
        for client in installed:
            _print_client_block(client, command)
        return 0

    if not ns.client:
        # No client specified and no --list/--detect — print all.
        for name, _desc in _SUPPORTED_CLIENTS:
            _print_client_block(name, command)
        return 0

    _print_client_block(ns.client, command)
    return 0


def _print_client_block(client: str, command: str) -> None:
    desc = next((d for n, d in _SUPPORTED_CLIENTS if n == client), client)
    bar = "─" * max(8, len(desc) + 2)
    print(f"\n{bar}\n {desc}\n{bar}\n")
    print(_block_for(client, command))


if __name__ == "__main__":
    sys.exit(main())
