"""Odoo domain filter sandbox.

Odoo's domain language is a prefix-logic expression over ``(field, op, value)``
leaves with ``'&'``, ``'|'``, ``'!'`` as operators. It is powerful: in
particular, a ``field`` can be a **dotted path** like ``create_uid.login`` that
traverses relational fields into other models. This is the single biggest
reason the MCP's model allowlist is not by itself sufficient — a search on
``res.partner`` with the domain ``[('create_uid.login', '=', 'admin')]`` would
happily reach into ``res.users`` even though ``res.users`` is not allowlisted.

This module enforces a strict subset of the domain language:

* Leaves must be 3-tuples ``(field, operator, value)``.
* ``field`` must be a simple (undotted) attribute on the target model.
* ``operator`` must be in :data:`_ALLOWED_OPS`.
* ``value`` must be ``None``, ``bool``, ``int``, ``float``, ``str``, or a
  homogeneous list of those scalars.
* The total number of leaves must not exceed :data:`_MAX_LEAVES`.
* Logical operators must be ``'&'``, ``'|'``, or ``'!'`` and must respect
  their arity (``&`` / ``|`` consume two subsequent expressions, ``!``
  consumes one) — otherwise Odoo's polish-notation parser would silently
  accept a malformed expression.

The caller is expected to have already validated the model against the
allowlist. This sandbox is defense in depth.
"""

from __future__ import annotations

from typing import Any, Final

from ..errors import DomainSandboxError

_ALLOWED_OPS: Final[frozenset[str]] = frozenset(
    {
        "=",
        "!=",
        ">",
        ">=",
        "<",
        "<=",
        "=?",
        "=like",
        "like",
        "not like",
        "ilike",
        "not ilike",
        "in",
        "not in",
        "child_of",
        "parent_of",
    }
)

_LOGICAL_OPS: Final[frozenset[str]] = frozenset({"&", "|", "!"})
_MAX_LEAVES: Final[int] = 32
_MAX_VALUE_LIST_LEN: Final[int] = 200


def sandbox_domain(domain: Any, known_fields: frozenset[str]) -> list[Any]:
    """Validate and return a normalized copy of ``domain``.

    ``known_fields`` is the set of fields on the target model (as returned by
    ``fields_get``). Only those are permitted as leaf field names.

    Returns a list that is safe to pass straight to ``search_read``. The
    returned list is a fresh copy — the caller's input is never mutated.
    """
    if not isinstance(domain, list):
        raise DomainSandboxError(
            f"Domain must be a list, got {type(domain).__name__}."
        )
    if len(domain) == 0:
        return []

    normalized: list[Any] = []
    leaf_count = 0
    for element in domain:
        if isinstance(element, str):
            if element not in _LOGICAL_OPS:
                raise DomainSandboxError(
                    f"Logical operator {element!r} not allowed. "
                    f"Use one of: {sorted(_LOGICAL_OPS)}"
                )
            normalized.append(element)
            continue

        if isinstance(element, (list, tuple)):
            leaf_count += 1
            if leaf_count > _MAX_LEAVES:
                raise DomainSandboxError(
                    f"Domain has more than {_MAX_LEAVES} leaves — refusing."
                )
            normalized.append(_validate_leaf(element, known_fields))
            continue

        raise DomainSandboxError(
            f"Unexpected element in domain: {element!r} ({type(element).__name__})."
        )

    _validate_polish_arity(normalized)
    return normalized


def _validate_leaf(leaf: Any, known_fields: frozenset[str]) -> tuple[str, str, Any]:
    if len(leaf) != 3:
        raise DomainSandboxError(f"Domain leaf must be a 3-tuple, got {leaf!r}.")
    field, operator, value = leaf

    if not isinstance(field, str) or not field:
        raise DomainSandboxError(f"Leaf field must be a non-empty string, got {field!r}.")
    if "." in field:
        # This is THE main thing we're guarding against.
        raise DomainSandboxError(
            f"Dotted field traversal in domains is not allowed: {field!r}. "
            f"This protects against cross-model reads (e.g. 'create_uid.login'). "
            f"Query the related model directly instead."
        )
    if field not in known_fields:
        raise DomainSandboxError(
            f"Field {field!r} does not exist on the target model. "
            f"Use odoo_describe_model to see available fields."
        )

    if not isinstance(operator, str) or operator not in _ALLOWED_OPS:
        raise DomainSandboxError(
            f"Operator {operator!r} is not allowed. Allowed: {sorted(_ALLOWED_OPS)}"
        )

    _validate_value(value)
    return (field, operator, value)


def _validate_value(value: Any) -> None:
    if value is None or isinstance(value, (bool, int, float, str)):
        return
    if isinstance(value, list):
        if len(value) > _MAX_VALUE_LIST_LEN:
            raise DomainSandboxError(
                f"Value list has {len(value)} items, max is {_MAX_VALUE_LIST_LEN}."
            )
        for item in value:
            if item is None or isinstance(item, (bool, int, float, str)):
                continue
            raise DomainSandboxError(
                f"Value list contains non-scalar item: {item!r} ({type(item).__name__})."
            )
        return
    raise DomainSandboxError(
        f"Value must be scalar or list of scalars, got {type(value).__name__}."
    )


def _validate_polish_arity(domain: list[Any]) -> None:
    """Verify that the polish-notation expression is well-formed.

    We walk the list simulating a stack: each logical operator consumes the
    right number of subsequent expressions. If the stack doesn't reduce to
    exactly one expression at the end, the domain is malformed.
    """
    # Odoo convention: implicit AND of all leaves, so a bare list of leaves is
    # always valid. The only time polish arity matters is when explicit & / |
    # / ! are present.
    has_logical = any(isinstance(e, str) for e in domain)
    if not has_logical:
        return

    # Walk right-to-left building a count. Each leaf contributes 1. '&' and
    # '|' require 2 subsequent expressions and produce 1. '!' requires 1 and
    # produces 1.
    count = 0
    for element in reversed(domain):
        if isinstance(element, str):
            if element in ("&", "|"):
                if count < 2:
                    raise DomainSandboxError(
                        f"Logical operator {element!r} has fewer than 2 operands."
                    )
                count -= 1  # consumes 2, produces 1
            elif element == "!":
                if count < 1:
                    raise DomainSandboxError("'!' operator has no operand.")
                # consumes 1, produces 1 — count unchanged
        else:
            count += 1
    if count != 1:
        raise DomainSandboxError(
            f"Malformed domain: expected a single top-level expression, got {count}."
        )
