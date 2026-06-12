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
#
# Adding a row grants AI agents the ability to call that Odoo method on
# production records (after dry-run + confirmation token). Each row is
# a deliberate security decision — verify the method exists on the
# supported Odoo versions (17.0+) and pin a test in
# tests/test_run_document_action.py so a refactor can't silently drop
# it. We never invent method names.
_DOCUMENT_ACTIONS: Final[dict[tuple[str, str], str]] = {
    ("purchase.order", "confirm"): "button_confirm",
    ("purchase.order", "cancel"): "button_cancel",
    ("sale.order", "confirm"): "action_confirm",
    ("sale.order", "cancel"): "action_cancel",
    ("account.move", "post"): "action_post",
    ("account.move", "cancel"): "button_cancel",
    ("stock.picking", "validate"): "button_validate",
    ("stock.picking", "cancel"): "action_cancel",
    # --- v0.20.0: cancel expansions for logistics, payments and HR -----
    # mrp.production.action_cancel — manufacturing order cancel. Odoo
    # rolls back component reservations and any in-flight workorders.
    ("mrp.production", "cancel"): "action_cancel",
    # account.payment.action_cancel — revokes a registered payment.
    # Odoo un-reconciles linked moves; the originating invoice returns
    # to "open". A real accounting event; the operator's dry-run review
    # is the safety net.
    ("account.payment", "cancel"): "action_cancel",
    # hr.leave.action_cancel — user-side withdraw of an own time-off
    # request. Distinct from ``action_refuse`` (manager-side rejection),
    # which is deliberately NOT exposed — refusing someone else's leave
    # is an HR decision that should go through the UI, not an agent.
    ("hr.leave", "cancel"): "action_cancel",
    # hr.expense.sheet.action_cancel — cancels an entire expense
    # report. We intentionally do NOT expose individual hr.expense
    # cancel: Odoo manages expense state through the parent sheet, and
    # a per-line cancel would put the sheet in an inconsistent state.
    ("hr.expense.sheet", "cancel"): "action_cancel",
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
