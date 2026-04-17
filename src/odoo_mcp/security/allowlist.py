"""Model and operation allowlist.

The dispatcher calls :func:`check_model` on every inbound tool call. The set
of allowed models comes from :class:`odoo_mcp.config.InstanceConfig`, so each
instance can override the default if needed.

Operations are a closed set defined here. Nothing outside it is exposed —
specifically, no ``unlink``, no ``execute_kw`` for arbitrary methods, no
``copy`` / ``name_search`` / ``fields_view_get``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from ..errors import ModelNotAllowedError, OperationNotAllowedError


class Operation(StrEnum):
    """The only operations the MCP is allowed to execute against Odoo."""

    SEARCH_READ = "search_read"
    SEARCH_COUNT = "search_count"
    READ = "read"
    READ_GROUP = "read_group"
    CREATE = "create"
    WRITE = "write"
    FIELDS_GET = "fields_get"  # used only by odoo_describe_model


_READ_OPS: Final[frozenset[Operation]] = frozenset(
    {
        Operation.SEARCH_READ,
        Operation.SEARCH_COUNT,
        Operation.READ,
        Operation.READ_GROUP,
        Operation.FIELDS_GET,
    }
)
_WRITE_OPS: Final[frozenset[Operation]] = frozenset({Operation.CREATE, Operation.WRITE})


def is_write(op: Operation) -> bool:
    return op in _WRITE_OPS


def is_read(op: Operation) -> bool:
    return op in _READ_OPS


def check_model(model: str, allowed: frozenset[str]) -> None:
    """Raise :class:`ModelNotAllowedError` if ``model`` is not in ``allowed``.

    Matching is exact and case-sensitive. Odoo model names are lowercase by
    convention and dotted (e.g. ``res.partner``), so any deviation is almost
    certainly a mistake and should fail loudly.
    """
    if not isinstance(model, str) or not model:
        raise ModelNotAllowedError("Model name must be a non-empty string.")
    # Reject anything that looks like an injection attempt — model names are
    # lowercase, dotted identifiers. Don't accept slashes, quotes, whitespace.
    for ch in model:
        if not (ch.isalnum() or ch in "._"):
            raise ModelNotAllowedError(f"Model name {model!r} contains invalid characters.")
    if model not in allowed:
        raise ModelNotAllowedError(
            f"Model {model!r} is not on the allowlist for this instance. "
            f"Allowed models: {sorted(allowed)}"
        )


def check_operation(op: Operation | str) -> Operation:
    """Coerce ``op`` to a validated :class:`Operation` or raise."""
    if isinstance(op, Operation):
        return op
    try:
        return Operation(op)
    except ValueError as exc:
        raise OperationNotAllowedError(
            f"Operation {op!r} is not exposed by this MCP. Allowed: {[o.value for o in Operation]}"
        ) from exc
