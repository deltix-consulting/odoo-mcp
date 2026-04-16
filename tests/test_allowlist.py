"""Tests for the model and operation allowlist."""

from __future__ import annotations

import pytest

from odoo_mcp.errors import ModelNotAllowedError, OperationNotAllowedError
from odoo_mcp.security.allowlist import Operation, check_model, check_operation, is_write


def test_check_model_accepts_exact_match() -> None:
    allowed = frozenset({"res.partner", "crm.lead"})
    check_model("res.partner", allowed)
    check_model("crm.lead", allowed)


def test_check_model_rejects_unknown() -> None:
    allowed = frozenset({"res.partner"})
    with pytest.raises(ModelNotAllowedError):
        check_model("res.users", allowed)


def test_check_model_rejects_case_variation() -> None:
    allowed = frozenset({"res.partner"})
    with pytest.raises(ModelNotAllowedError):
        check_model("Res.Partner", allowed)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "res/partner",
        "res partner",
        "res;partner",
        "res.partner\n",
        "res'partner",
    ],
)
def test_check_model_rejects_invalid_characters(bad: str) -> None:
    allowed = frozenset({"res.partner"})
    with pytest.raises(ModelNotAllowedError):
        check_model(bad, allowed)


def test_check_operation_accepts_enum_values() -> None:
    assert check_operation("search_read") is Operation.SEARCH_READ
    assert check_operation("create") is Operation.CREATE
    assert check_operation(Operation.WRITE) is Operation.WRITE


@pytest.mark.parametrize("bad", ["unlink", "copy", "execute_kw", "name_search", "", "UNLINK"])
def test_check_operation_rejects_everything_else(bad: str) -> None:
    with pytest.raises(OperationNotAllowedError):
        check_operation(bad)


def test_is_write_classification() -> None:
    assert is_write(Operation.CREATE)
    assert is_write(Operation.WRITE)
    assert not is_write(Operation.SEARCH_READ)
    assert not is_write(Operation.READ)
    assert not is_write(Operation.FIELDS_GET)
