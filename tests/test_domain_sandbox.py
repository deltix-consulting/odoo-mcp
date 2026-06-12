"""Tests for the domain sandbox.

The most important tests here are the ones that block cross-model field
traversal — that is the attack the allowlist alone does not prevent.
"""

from __future__ import annotations

import pytest

from odoo_mcp.errors import DomainSandboxError
from odoo_mcp.security.domain import sandbox_domain

FIELDS = frozenset({"name", "active", "email", "create_uid", "id", "partner_id", "state"})


def test_empty_domain_is_ok() -> None:
    assert sandbox_domain([], FIELDS) == []


def test_simple_leaf_is_ok() -> None:
    out = sandbox_domain([("name", "ilike", "acme")], FIELDS)
    assert out == [("name", "ilike", "acme")]


def test_multiple_leaves_implicit_and_is_ok() -> None:
    domain = [("name", "ilike", "acme"), ("active", "=", True)]
    assert sandbox_domain(domain, FIELDS) == domain


def test_explicit_polish_and_is_ok() -> None:
    domain = ["&", ("name", "ilike", "acme"), ("active", "=", True)]
    assert sandbox_domain(domain, FIELDS) == domain


def test_or_and_not_accepted() -> None:
    domain = ["|", ("name", "=", "a"), "!", ("active", "=", False)]
    assert sandbox_domain(domain, FIELDS) == domain


# ---------------------------------------------------------------------------
# The critical security tests — cross-model field traversal
# ---------------------------------------------------------------------------


def test_dotted_field_is_rejected_even_for_known_relation() -> None:
    """`create_uid.login` is THE footgun we're protecting against."""
    with pytest.raises(DomainSandboxError, match="Dotted field"):
        sandbox_domain([("create_uid.login", "=", "admin")], FIELDS)


def test_deeply_dotted_field_is_rejected() -> None:
    with pytest.raises(DomainSandboxError, match="Dotted field"):
        sandbox_domain([("partner_id.user_id.login", "=", "admin")], FIELDS)


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(DomainSandboxError, match="does not exist"):
        sandbox_domain([("not_a_field", "=", "x")], FIELDS)


@pytest.mark.parametrize(
    "bad_op",
    [
        "is",  # not in whitelist
        "IS",  # case must match
        "=raw",
        "SELECT",
        "",
        "between",
    ],
)
def test_unknown_operator_is_rejected(bad_op: str) -> None:
    with pytest.raises(DomainSandboxError, match="Operator"):
        sandbox_domain([("name", bad_op, "x")], FIELDS)


def test_non_scalar_value_rejected() -> None:
    with pytest.raises(DomainSandboxError, match="scalar"):
        sandbox_domain([("name", "=", {"nested": "dict"})], FIELDS)


def test_scalar_list_value_ok() -> None:
    assert sandbox_domain([("state", "in", ["draft", "open"])], FIELDS) == [
        ("state", "in", ["draft", "open"])
    ]


def test_list_with_non_scalar_rejected() -> None:
    with pytest.raises(DomainSandboxError, match="non-scalar"):
        sandbox_domain([("state", "in", ["draft", {"x": 1}])], FIELDS)


def test_too_many_leaves_rejected() -> None:
    domain = [("name", "=", f"x{i}") for i in range(33)]
    with pytest.raises(DomainSandboxError, match="more than"):
        sandbox_domain(domain, FIELDS)


def test_huge_value_list_rejected() -> None:
    big = list(range(201))
    with pytest.raises(DomainSandboxError, match="max is"):
        sandbox_domain([("id", "in", big)], FIELDS)


def test_non_list_domain_rejected() -> None:
    with pytest.raises(DomainSandboxError):
        sandbox_domain("name=acme", FIELDS)  # type: ignore[arg-type]


def test_malformed_leaf_tuple_rejected() -> None:
    with pytest.raises(DomainSandboxError, match="3-tuple"):
        sandbox_domain([("name", "=")], FIELDS)  # type: ignore[list-item]


def test_unknown_logical_operator_rejected() -> None:
    with pytest.raises(DomainSandboxError, match="Logical operator"):
        sandbox_domain(["xor", ("name", "=", "x"), ("id", "=", 1)], FIELDS)


def test_malformed_polish_expression_rejected() -> None:
    # '&' needs two operands but only one follows.
    with pytest.raises(DomainSandboxError):
        sandbox_domain(["&", ("name", "=", "x")], FIELDS)


def test_implicit_and_mixed_with_explicit_or_is_ok() -> None:
    """Odoo's normalize_domain joins leftover top-level expressions with
    an implicit AND, so a leaf followed by an OR-pair is a valid domain.
    This shape used to be wrongly rejected (v0.21.0 and earlier)."""
    domain = [("active", "=", True), "|", ("name", "=", "a"), ("email", "=", "b")]
    assert sandbox_domain(domain, FIELDS) == domain


def test_explicit_or_followed_by_implicit_and_leaf_is_ok() -> None:
    domain = ["|", ("name", "=", "a"), ("email", "=", "b"), ("active", "=", True)]
    assert sandbox_domain(domain, FIELDS) == domain


def test_multiple_or_groups_implicit_and_is_ok() -> None:
    domain = [
        "|",
        ("name", "=", "a"),
        ("email", "=", "b"),
        "|",
        ("state", "=", "draft"),
        ("active", "=", True),
    ]
    assert sandbox_domain(domain, FIELDS) == domain


def test_trailing_underfed_operator_still_rejected() -> None:
    with pytest.raises(DomainSandboxError, match="fewer than 2"):
        sandbox_domain([("name", "=", "x"), "|", ("active", "=", True)], FIELDS)


def test_dotted_rejection_suggests_two_call_pattern() -> None:
    with pytest.raises(DomainSandboxError, match="two calls"):
        sandbox_domain([("partner_id.name", "=", "Acme")], FIELDS)
