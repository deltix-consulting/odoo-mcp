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
* A relational ``field`` (many2one / one2many / many2many) must be compared
  to ids, ``False`` or ``None`` — never to a string. Odoo turns a string on a
  relational field into a search on the RELATED model's display name, which
  is the dotted traversal above in another spelling.
* ``operator`` must be in :data:`_ALLOWED_OPS`.
* ``value`` must be ``None``, ``bool``, ``int``, ``float``, ``str``, or a
  homogeneous list of those scalars.
* The total number of leaves must not exceed :data:`_MAX_LEAVES`.
* Logical operators must be ``'&'``, ``'|'``, or ``'!'`` and must respect
  their arity (``&`` / ``|`` consume two subsequent expressions, ``!``
  consumes one) — otherwise Odoo's polish-notation parser would silently
  accept a malformed expression. Multiple top-level expressions are fine:
  Odoo joins them with an implicit AND (``normalize_domain`` prepends the
  missing ``'&'`` operators), so ``[leaf, '|', leaf, leaf]`` is valid.

The caller is expected to have already validated the model against the
allowlist. This sandbox is defense in depth.
"""

from __future__ import annotations

import re
from typing import Any, Final

from ..errors import DomainSandboxError
from .fields import is_always_redacted_with_extra, is_default_hidden

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


def sandbox_domain(
    domain: Any,
    known_fields: frozenset[str],
    *,
    model: str | None = None,
    allow_sensitive: frozenset[str] = frozenset(),
    instance_overrides: dict[str, frozenset[str]] | None = None,
    extra_redacted: tuple[re.Pattern[str], ...] = (),
    relational_fields: frozenset[str] = frozenset(),
) -> list[Any]:
    """Validate and return a normalized copy of ``domain``.

    ``known_fields`` is the set of fields on the target model (as returned by
    ``fields_get``). Only those are permitted as leaf field names.

    **Redaction policy on filter fields.** When ``model`` is supplied, each
    leaf's field is run through the same redaction policy the read path uses:
    an always-redacted field (``password`` / ``*_token`` / ``*_secret`` / the
    salary-family patterns) can never be filtered on, and a default-hidden
    field (VAT, IBAN, SSN, wage, ...) requires the caller to opt in via
    ``allow_sensitive``. This closes the search/count *oracle*: filtering on a
    field you cannot read (e.g. ``[('wage','>',5000)]`` with ``search_count``,
    or ``[('access_token','=like','abc%')]``) would otherwise let a caller
    recover a hidden value one comparison at a time without any row ever being
    returned for redaction to act on. Grouping is already blocked for the same
    reason (:func:`odoo_mcp.security.fields.validate_groupby`); domains are the
    other value-revealing surface.

    **Strings on relational fields.** ``relational_fields`` names the
    target model's many2one / one2many / many2many fields. A leaf on one of
    them whose value is a string (or a list holding one) is refused: Odoo
    rewrites ``('partner_id', 'ilike', 'x')`` as ``('partner_id', 'any',
    [('display_name', 'ilike', 'x')])`` (16.0-18.0 through ``_name_search`` /
    ``_search`` on ``display_name``, 19.0+ in ``_optimize_relational_name_search``),
    and the related model's ``_search_display_name`` matches every field in
    its ``_rec_names_search``. On ``res.partner`` that list is ``vat`` and
    ``company_registry`` too (16.0-20.0), both default-hidden here — so a
    string on ``partner_id`` is a prefix oracle on a hidden field of a model
    the caller never named, with the same reach as the dotted path the sandbox
    refuses. ``child_of`` / ``parent_of`` with a string value take the same
    route. Ids are the supported form, as for dotted paths.

    When ``model`` is ``None`` the policy check is skipped — this is the bare
    defense-in-depth call shape used by unit tests, where the model + field
    allowlisting has been asserted separately. All dispatcher call sites pass
    ``model``.

    Returns a list that is safe to pass straight to ``search_read``. The
    returned list is a fresh copy — the caller's input is never mutated.
    """
    if not isinstance(domain, list):
        raise DomainSandboxError(f"Domain must be a list, got {type(domain).__name__}.")
    if len(domain) == 0:
        return []

    normalized: list[Any] = []
    leaf_count = 0
    for element in domain:
        if isinstance(element, str):
            if element not in _LOGICAL_OPS:
                raise DomainSandboxError(
                    f"Logical operator {element!r} not allowed. Use one of: {sorted(_LOGICAL_OPS)}"
                )
            normalized.append(element)
            continue

        if isinstance(element, (list, tuple)):
            leaf_count += 1
            if leaf_count > _MAX_LEAVES:
                raise DomainSandboxError(f"Domain has more than {_MAX_LEAVES} leaves — refusing.")
            normalized.append(
                _validate_leaf(
                    element,
                    known_fields,
                    model=model,
                    allow_sensitive=allow_sensitive,
                    instance_overrides=instance_overrides,
                    extra_redacted=extra_redacted,
                    relational_fields=relational_fields,
                )
            )
            continue

        raise DomainSandboxError(
            f"Unexpected element in domain: {element!r} ({type(element).__name__})."
        )

    _validate_polish_arity(normalized)
    return normalized


def _validate_leaf(
    leaf: Any,
    known_fields: frozenset[str],
    *,
    model: str | None = None,
    allow_sensitive: frozenset[str] = frozenset(),
    instance_overrides: dict[str, frozenset[str]] | None = None,
    extra_redacted: tuple[re.Pattern[str], ...] = (),
    relational_fields: frozenset[str] = frozenset(),
) -> tuple[str, str, Any]:
    if len(leaf) != 3:
        raise DomainSandboxError(f"Domain leaf must be a 3-tuple, got {leaf!r}.")
    field, operator, value = leaf

    if not isinstance(field, str) or not field:
        raise DomainSandboxError(f"Leaf field must be a non-empty string, got {field!r}.")
    if "." in field:
        # This is THE main thing we're guarding against.
        base = field.split(".", 1)[0]
        raise DomainSandboxError(
            f"Dotted field traversal in domains is not allowed: {field!r}. "
            f"This protects against cross-model reads (e.g. 'create_uid.login'). "
            f"Do it in two calls instead: (1) find the matching ids on the "
            f"related model with odoo_lookup or odoo_search_read, then "
            f"(2) re-run this call filtering on ({base!r}, 'in', [<those ids>])."
        )
    if field not in known_fields:
        raise DomainSandboxError(
            f"Field {field!r} does not exist on the target model. "
            f"Use odoo_describe_model to see available fields."
        )

    # Redaction policy on the filter field (see sandbox_domain docstring).
    # Only enforced when the caller supplies the model context.
    if model is not None:
        if is_always_redacted_with_extra(field, extra_redacted):
            raise DomainSandboxError(
                f"Field {field!r} is permanently redacted and cannot be used as a "
                f"filter — filtering on it would leak its value via the match/count "
                f"result even though the value is never returned."
            )
        if is_default_hidden(model, field, instance_overrides=instance_overrides) and (
            field not in allow_sensitive
        ):
            raise DomainSandboxError(
                f"Field {field!r} on {model!r} is sensitive and cannot be used as a "
                f"filter without opting in: filtering reveals its values through the "
                f"result set. Pass allow_sensitive_fields=[{field!r}, ...] to unlock."
            )

    if not isinstance(operator, str) or operator not in _ALLOWED_OPS:
        raise DomainSandboxError(
            f"Operator {operator!r} is not allowed. Allowed: {sorted(_ALLOWED_OPS)}"
        )

    _validate_value(value)
    if field in relational_fields and _holds_string(value):
        # See "Strings on relational fields" in the sandbox_domain docstring.
        raise DomainSandboxError(
            f"Field {field!r} is relational: compare it to record ids, not to a name "
            f"({value!r}). Odoo would match the text against the related model's "
            f"display name and its other name-search fields (on contacts: VAT and "
            f"company registry), which reaches into that model the way a dotted path "
            f"does. Do it in two calls instead: (1) find the ids on the related model "
            f"with odoo_lookup or odoo_search_read, then (2) re-run this call filtering "
            f"on ({field!r}, 'in', [<those ids>])."
        )
    return (field, operator, value)


def _holds_string(value: Any) -> bool:
    """Whether a leaf value is, or contains, a string."""
    if isinstance(value, str):
        return True
    return isinstance(value, list) and any(isinstance(item, str) for item in value)


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
    right number of subsequent expressions. Odoo joins any number of
    leftover top-level expressions with an implicit AND (its
    ``normalize_domain`` prepends the missing ``'&'`` operators), so ending
    with *more* than one expression is valid — only an operator that
    can't find its operands makes the domain malformed.
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
    # count > 1 is fine (implicit AND of the leftover expressions); the
    # operator checks above already rejected every underfed & / | / !.
    # count < 1 can't happen for a non-empty domain, but fail closed anyway.
    if count < 1:
        raise DomainSandboxError(
            "Malformed domain: no top-level expression left after applying operators."
        )
