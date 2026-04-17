"""Tests for server-level behavior: audit args shape and sanitization."""

from __future__ import annotations

from odoo_mcp.server import _args_shape, _sanitize_details


def test_args_shape_captures_field_names_not_values() -> None:
    shape = _args_shape(
        {
            "instance": "dev",
            "model": "res.partner",
            "domain": [["active", "=", True], "&", ["name", "ilike", "acme"]],
            "fields": ["id", "name", "email"],
            "allow_sensitive_fields": ["vat"],
            "limit": 50,
        }
    )
    assert shape["instance"] == "dev"
    assert shape["model"] == "res.partner"
    assert shape["field_count"] == 3
    assert shape["field_names"] == ["email", "id", "name"]
    # Leaves: two list-tuples; operators: one string.
    assert shape["domain_leaves"] == 2
    assert shape["domain_operators"] == 1
    # allow_sensitive_fields contents are replaced with a count.
    assert shape["allow_sensitive_count"] == 1
    assert "allow_sensitive_fields" not in shape
    assert shape["limit"] == 50
    # The raw 'acme' value from the domain must not appear anywhere.
    assert "acme" not in str(shape)


def test_args_shape_ids_and_values_are_counted_not_embedded() -> None:
    shape = _args_shape(
        {
            "instance": "dev",
            "model": "res.partner",
            "ids": [101, 102, 103],
            "values": {"name": "Acme", "vat": "BE1234"},
            "confirmation_token": "abc123",
        }
    )
    assert shape["id_count"] == 3
    assert "ids" not in shape
    assert shape["value_count"] == 2
    assert shape["value_keys"] == ["name", "vat"]
    # Neither the ids themselves nor the written values leak into the shape.
    serialized = str(shape)
    assert "101" not in serialized
    assert "Acme" not in serialized
    assert "BE1234" not in serialized
    # confirmation_token is present/absent only.
    assert shape["confirmation_token_present"] is True
    assert "abc123" not in serialized


def test_args_shape_groupby_specs_preserved() -> None:
    shape = _args_shape(
        {
            "instance": "dev",
            "model": "crm.lead",
            "groupby": ["stage_id", "create_date:month"],
            "fields": ["expected_revenue:sum"],
        }
    )
    assert shape["groupby_count"] == 2
    assert shape["groupby_specs"] == ["stage_id", "create_date:month"]


def test_args_shape_unknown_arg_records_presence_and_type() -> None:
    shape = _args_shape({"weird": {"nested": "thing"}})
    assert shape["weird"] == {"present": True, "type": "dict"}


def test_sanitize_details_keeps_one_level_of_nesting() -> None:
    raw = {
        "record_count": 5,
        "args": {"model": "res.partner", "field_count": 2},
        "bad_deep": {"inner": {"too": "deep"}},  # inner-inner dict is dropped
    }
    out = _sanitize_details(raw)
    assert out["record_count"] == 5
    assert out["args"] == {"model": "res.partner", "field_count": 2}
    # The doubly-nested dict is filtered — its 'inner' key maps to a dict,
    # which isn't a leaf, so it gets stripped.
    assert out["bad_deep"] == {}


def test_sanitize_details_drops_arbitrary_objects() -> None:
    class Random:
        pass

    raw = {"keep": "me", "drop": Random(), "also_keep": ["a", "b"]}
    out = _sanitize_details(raw)
    assert out["keep"] == "me"
    assert out["also_keep"] == ["a", "b"]
    assert "drop" not in out
