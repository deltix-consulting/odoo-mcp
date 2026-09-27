"""MCP tool annotations must tell the client the truth about each tool.

Clients act on these hints without asking the server again: Claude Code runs
``readOnlyHint`` tools in parallel and other hosts skip the approval prompt
for them. A write tool marked read-only would be called without the human
seeing it, so the hint is pinned to the dispatcher's own definition of a
write path: the handlers that call ``_refuse_if_read_only_session``.
"""

from __future__ import annotations

import pytest
from mcp.types import Tool

from odoo_mcp.dispatcher import _HANDLERS
from odoo_mcp.tools import build_tools

_TOOLS: dict[str, Tool] = {t.name: t for t in build_tools()}


def _is_write_path(tool_name: str) -> bool:
    # ``co_names`` is the compiled name table, so a comment or a docstring
    # mentioning the helper does not count as a call.
    return "_refuse_if_read_only_session" in _HANDLERS[tool_name].__code__.co_names


def test_every_handler_has_a_tool_and_every_tool_a_handler() -> None:
    assert set(_TOOLS) == set(_HANDLERS)


@pytest.mark.parametrize("name", sorted(_TOOLS))
def test_every_tool_declares_read_only_and_open_world(name: str) -> None:
    ann = _TOOLS[name].annotations
    assert ann is not None, f"{name} has no annotations"
    # The spec defaults are readOnlyHint=False and openWorldHint=True.
    # Leaving either unset makes a closed read tool look like a web search.
    assert ann.readOnlyHint is not None
    assert ann.openWorldHint is not None


@pytest.mark.parametrize("name", sorted(_TOOLS))
def test_read_only_hint_matches_the_read_only_session_gate(name: str) -> None:
    ann = _TOOLS[name].annotations
    assert ann is not None
    assert ann.readOnlyHint is (not _is_write_path(name)), (
        f"{name}: readOnlyHint={ann.readOnlyHint} but the handler "
        f"{'calls' if _is_write_path(name) else 'does not call'} "
        "_refuse_if_read_only_session"
    )


def test_the_gate_classifies_at_least_the_known_write_tools() -> None:
    # Guards the guard: if the helper were renamed, _is_write_path would be
    # False everywhere and every tool would have to claim read-only to pass.
    writes = {n for n in _TOOLS if _is_write_path(n)}
    assert {
        "odoo_create",
        "odoo_write",
        "odoo_archive_or_delete",
        "odoo_send_message",
        "odoo_log_note",
        "odoo_run_document_action",
        "odoo_create_attachment",
        "odoo_enable_prod_writes",
    } <= writes


@pytest.mark.parametrize("name", sorted(n for n in _TOOLS if _is_write_path(n)))
def test_write_tools_state_destructive_and_are_never_idempotent(name: str) -> None:
    ann = _TOOLS[name].annotations
    assert ann is not None
    assert ann.destructiveHint is not None, f"{name} leaves destructiveHint to the default"
    # A confirmation token is single use, and a write whose transport failed
    # may have committed: a client must never retry one on its own.
    assert ann.idempotentHint is False


def test_tools_that_remove_or_overwrite_data_are_destructive() -> None:
    for name in ("odoo_write", "odoo_archive_or_delete", "odoo_run_document_action"):
        ann = _TOOLS[name].annotations
        assert ann is not None and ann.destructiveHint is True, name
    for name in ("odoo_create", "odoo_log_note", "odoo_create_attachment"):
        ann = _TOOLS[name].annotations
        assert ann is not None and ann.destructiveHint is False, name


def test_only_tools_that_can_email_someone_are_open_world() -> None:
    open_world = {n for n, t in _TOOLS.items() if t.annotations and t.annotations.openWorldHint}
    assert open_world == {"odoo_send_message", "odoo_run_document_action"}


def test_annotations_reach_the_wire() -> None:
    # What the client receives from tools/list, not the Python object.
    wire = {
        t["name"]: t
        for t in (tool.model_dump(mode="json", by_alias=True) for tool in build_tools())
    }
    assert wire["odoo_search_read"]["annotations"]["readOnlyHint"] is True
    assert wire["odoo_archive_or_delete"]["annotations"]["destructiveHint"] is True
