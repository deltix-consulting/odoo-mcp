"""A rejected field name must tell the agent how to recover.

Every validator that rejects an unknown field already holds the model's
full valid field set, so naming the near-misses costs no round trip. The
security half is what the suggester must NOT name: ``known_fields`` still
contains always-redacted fields, which ``odoo_describe_model`` is
documented to never advertise.
"""

from __future__ import annotations

import re

import pytest

from odoo_mcp.errors import DomainSandboxError, FieldPolicyError
from odoo_mcp.security.domain import sandbox_domain
from odoo_mcp.security.fields import (
    suggest_field_names,
    validate_aggregate_fields,
    validate_groupby,
    validate_order,
    validate_requested_fields,
    validate_write_values,
)

# An ordinary (non-whitelisted) model: restrict_fields_meta passes every
# name through, so known_fields carries the always-redacted ones too.
MODEL = "res.partner"
KNOWN = frozenset({"name", "ref", "vat", "access_token", "password_crypt", "amount_total"})
NO_SENSITIVE: frozenset[str] = frozenset()


def test_close_match_is_offered() -> None:
    hint = suggest_field_names("naem", KNOWN)
    assert "Did you mean: name?" in hint
    assert "odoo_describe_model" in hint


def test_no_close_match_still_points_at_describe_model() -> None:
    hint = suggest_field_names("zzzzzzzz", KNOWN)
    assert "Did you mean" not in hint
    assert hint == " Use odoo_describe_model to see available fields."


def test_at_most_three_suggestions() -> None:
    known = frozenset({f"amount_total_{i}" for i in range(10)})
    hint = suggest_field_names("amount_total_x", known)
    assert hint.count(",") <= 2


@pytest.mark.parametrize(
    "typo,secret", [("access_tokn", "access_token"), ("passwrd_crypt", "password_crypt")]
)
def test_always_redacted_field_is_never_suggested(typo: str, secret: str) -> None:
    """The trap: difflib over the raw known_fields would name these.

    ``redact_fields_get`` strips them from ``odoo_describe_model`` so the
    tool "never even advertises [their] existence"; the error path must
    not hand them back to a caller who guesses.
    """
    assert secret in KNOWN  # the validator does see it
    hint = suggest_field_names(typo, KNOWN)
    assert secret not in hint


def test_instance_configured_extra_redacted_is_never_suggested() -> None:
    extra = (re.compile(r"amount_total", re.IGNORECASE),)
    assert "amount_total" not in suggest_field_names("amount_totl", KNOWN, extra_redacted=extra)


def test_default_hidden_field_is_suggested() -> None:
    """vat is default-hidden, not always-redacted — describe_model lists it."""
    assert "vat" in suggest_field_names("vta", KNOWN)


# --- every rejection site carries the hint ---------------------------------


def test_requested_fields_rejection_suggests() -> None:
    with pytest.raises(FieldPolicyError, match="Did you mean: name?"):
        validate_requested_fields(MODEL, ["naem"], KNOWN, allow_sensitive=NO_SENSITIVE)


def test_write_values_rejection_suggests() -> None:
    with pytest.raises(FieldPolicyError, match="Did you mean: name?"):
        validate_write_values(MODEL, {"naem": "x"}, KNOWN)


def test_aggregate_rejection_suggests() -> None:
    with pytest.raises(FieldPolicyError, match="Did you mean: amount_total?"):
        validate_aggregate_fields(MODEL, ["amount_totl:sum"], KNOWN, allow_sensitive=NO_SENSITIVE)


def test_groupby_rejection_suggests() -> None:
    with pytest.raises(FieldPolicyError, match="Did you mean: name?"):
        validate_groupby(MODEL, ["naem"], KNOWN, allow_sensitive=NO_SENSITIVE)


def test_order_rejection_suggests() -> None:
    with pytest.raises(FieldPolicyError, match="Did you mean: name?"):
        validate_order(MODEL, "naem desc", KNOWN, allow_sensitive=NO_SENSITIVE)


def test_domain_rejection_suggests() -> None:
    with pytest.raises(DomainSandboxError, match="Did you mean: name?"):
        sandbox_domain([("naem", "=", "x")], KNOWN, model=MODEL)


def test_domain_no_match_message_is_unchanged() -> None:
    """The one site that already pointed at describe_model keeps its wording."""
    with pytest.raises(DomainSandboxError) as exc:
        sandbox_domain([("zzzzzzzz", "=", "x")], KNOWN, model=MODEL)
    assert str(exc.value) == (
        "Field 'zzzzzzzz' does not exist on the target model. "
        "Use odoo_describe_model to see available fields."
    )
