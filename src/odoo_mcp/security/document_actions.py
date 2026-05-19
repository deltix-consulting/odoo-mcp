"""Hardcoded map of document workflow actions to Odoo methods.

The single security boundary for the ``odoo_run_document_action`` tool.
A caller names a semantic action (``confirm`` / ``cancel`` / ``post`` /
``validate``) on a model; this map resolves it to the exact Odoo method
name. The caller can NEVER supply a method name — only a
``(model, action)`` pair that must be present in this map. Anything not
mapped is refused.

This is the same shape as ``odoo_archive_or_delete``'s mode -> method
choice: a small, audited, non-config-overridable lookup. It is NOT a
generic ``execute_kw`` surface — adding a row here is a deliberate code
change subject to security review, exactly like adding a
``MODEL_DENYLIST`` entry.

**Deliberately excluded:** reset-to-draft (``button_draft`` /
``action_draft``). Un-posting an invoice or reverting a confirmed order
has accounting and legal implications; if that is genuinely needed it
gets its own tool with its own review, not a quiet row in this map.
"""

from __future__ import annotations

from typing import Final

from ..errors import OperationNotAllowedError

# (model, action) -> Odoo method name. The map IS the allowlist.
_DOCUMENT_ACTIONS: Final[dict[tuple[str, str], str]] = {
    ("purchase.order", "confirm"): "button_confirm",
    ("purchase.order", "cancel"): "button_cancel",
    ("sale.order", "confirm"): "action_confirm",
    ("sale.order", "cancel"): "action_cancel",
    ("account.move", "post"): "action_post",
    ("account.move", "cancel"): "button_cancel",
    ("stock.picking", "validate"): "button_validate",
    ("stock.picking", "cancel"): "action_cancel",
}

# Action verbs the tool's schema advertises. Derived from the map so the
# two never drift.
DOCUMENT_ACTION_VERBS: Final[tuple[str, ...]] = tuple(
    sorted({action for (_model, action) in _DOCUMENT_ACTIONS})
)


def supported_pairs() -> list[str]:
    """Return ``model:action`` strings for every mapped pair, sorted."""
    return sorted(f"{model}:{action}" for (model, action) in _DOCUMENT_ACTIONS)


def resolve_document_action(model: str, action: str) -> str:
    """Return the Odoo method for ``(model, action)``, or raise.

    Raises :class:`OperationNotAllowedError` if the pair is not in the
    hardcoded map. The message lists every supported pair so the caller
    can correct without guessing.
    """
    method = _DOCUMENT_ACTIONS.get((model, action))
    if method is None:
        raise OperationNotAllowedError(
            f"No document action {action!r} is defined for model {model!r}. "
            f"Supported (model:action): {supported_pairs()}"
        )
    return method
