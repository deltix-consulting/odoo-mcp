"""Pin the MCP SDK major that ``server.build_server`` is written against.

``build_server`` registers its four handlers through the low-level
decorator API — ``Server.list_tools`` / ``.call_tool`` / ``.list_prompts``
/ ``.get_prompt``. The SDK's 2.x line rebuilt that API: handlers register
via ``add_request_handler`` and those four attributes are gone, so
``build_server`` raises ``AttributeError`` before the server can serve a
single request.

Two things have to hold together, and they fail in different places:

* ``pyproject.toml`` must keep the ``<2`` cap. ``uv.lock`` protects
  ``uv sync``, but ``uv tool install`` (``scripts/install.sh`` step 8)
  resolves from the declared constraint alone — an uncapped ``mcp>=1.2.0``
  puts a non-starting ``odoo-mcp`` on the user's PATH while the project
  venv stays green, so CI would never see it.
* The SDK actually resolved into this environment must still expose the
  decorators. That catches a future 1.x deprecation as well as a cap that
  someone widened without doing the handler-API migration.

Lift the cap only together with that migration.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from mcp.server import Server
from packaging.requirements import Requirement

# The decorators ``build_server`` calls. Keep in sync with
# ``odoo_mcp.server.build_server``.
REQUIRED_SERVER_DECORATORS = ("list_tools", "call_tool", "list_prompts", "get_prompt")

# The first major that dropped them.
INCOMPATIBLE_SDK_VERSION = "2.0.0"


def _mcp_requirement() -> Requirement:
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    raw = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    for dep in raw["project"]["dependencies"]:
        requirement = Requirement(dep)
        if requirement.name == "mcp":
            return requirement
    raise AssertionError("no 'mcp' entry in [project].dependencies")


def test_declared_mcp_constraint_excludes_the_rebuilt_major() -> None:
    """A fresh resolve must not be able to pick up an SDK 2.x."""
    specifier = _mcp_requirement().specifier
    assert not specifier.contains(INCOMPATIBLE_SDK_VERSION), (
        f"pyproject allows mcp {INCOMPATIBLE_SDK_VERSION}, which removed "
        f"{REQUIRED_SERVER_DECORATORS} from the low-level Server. "
        "'uv tool install' resolves from this constraint and ignores uv.lock, "
        "so widening it ships a CLI that raises AttributeError on startup. "
        "Lift the cap only together with the 2.x handler-API migration."
    )


def test_resolved_sdk_still_exposes_the_decorators_build_server_uses() -> None:
    """The SDK actually installed here must satisfy build_server's contract."""
    missing = [name for name in REQUIRED_SERVER_DECORATORS if not hasattr(Server, name)]
    assert not missing, (
        f"the resolved mcp SDK no longer exposes {missing} on the low-level "
        "Server; odoo_mcp.server.build_server cannot register its handlers "
        "and the server will not start."
    )
