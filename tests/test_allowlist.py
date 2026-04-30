"""Tests for the model and operation allowlist."""

from __future__ import annotations

import pytest

from odoo_mcp.errors import ModelNotAllowedError, OperationNotAllowedError
from odoo_mcp.security.allowlist import (
    ALLOWLIST_WILDCARD,
    MODEL_DENYLIST,
    Operation,
    check_model,
    check_operation,
    is_read,
    is_write,
)


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


@pytest.mark.parametrize("bad", ["copy", "execute_kw", "name_search", "", "UNLINK"])
def test_check_operation_rejects_everything_else(bad: str) -> None:
    with pytest.raises(OperationNotAllowedError):
        check_operation(bad)


def test_check_operation_accepts_archive_and_unlink() -> None:
    # Both are write ops used only by odoo_archive_or_delete.
    assert check_operation("archive") is Operation.ARCHIVE
    assert check_operation("unlink") is Operation.UNLINK


def test_is_write_classification() -> None:
    assert is_write(Operation.CREATE)
    assert is_write(Operation.WRITE)
    assert is_write(Operation.ARCHIVE)
    assert is_write(Operation.UNLINK)
    assert not is_write(Operation.SEARCH_READ)
    assert not is_write(Operation.SEARCH_COUNT)
    assert not is_write(Operation.READ)
    assert not is_write(Operation.READ_GROUP)
    assert not is_write(Operation.FIELDS_GET)


def test_is_read_classification() -> None:
    assert is_read(Operation.SEARCH_READ)
    assert is_read(Operation.SEARCH_COUNT)
    assert is_read(Operation.READ)
    assert is_read(Operation.READ_GROUP)
    assert is_read(Operation.FIELDS_GET)
    assert not is_read(Operation.CREATE)
    assert not is_read(Operation.WRITE)


def test_check_operation_accepts_new_aggregate_ops() -> None:
    assert check_operation("search_count") is Operation.SEARCH_COUNT
    assert check_operation("read_group") is Operation.READ_GROUP


# -- Denylist / open-mode -----------------------------------------------------


def test_denylist_blocks_res_users_even_in_open_mode() -> None:
    # Open mode = wildcard in the allowed set.
    allowed = frozenset({ALLOWLIST_WILDCARD})
    with pytest.raises(ModelNotAllowedError, match="denylist"):
        check_model("res.users", allowed)


def test_denylist_blocks_ir_config_parameter() -> None:
    allowed = frozenset({ALLOWLIST_WILDCARD})
    with pytest.raises(ModelNotAllowedError, match="denylist"):
        check_model("ir.config_parameter", allowed)


def test_denylist_blocks_even_in_strict_mode_override() -> None:
    # Safety invariant: even if a misguided user put res.users on their
    # strict allowlist, it should still be denied.
    allowed = frozenset({"res.users", "res.partner"})
    with pytest.raises(ModelNotAllowedError, match="denylist"):
        check_model("res.users", allowed)


def test_open_mode_allows_any_non_denied_model() -> None:
    allowed = frozenset({ALLOWLIST_WILDCARD})
    # None of these are on MODEL_DENYLIST — all should pass.
    check_model("res.partner", allowed)
    check_model("sale.order", allowed)
    check_model("some.custom.module", allowed)


def test_open_mode_still_validates_name_shape() -> None:
    allowed = frozenset({ALLOWLIST_WILDCARD})
    with pytest.raises(ModelNotAllowedError, match="invalid characters"):
        check_model("res partner", allowed)
    with pytest.raises(ModelNotAllowedError, match="invalid characters"):
        check_model("res.partner;drop", allowed)
    with pytest.raises(ModelNotAllowedError, match="non-empty string"):
        check_model("", allowed)


def test_strict_mode_unchanged() -> None:
    # A pre-v0.4 strict list still works: only listed models are allowed.
    allowed = frozenset({"res.partner", "crm.lead"})
    check_model("res.partner", allowed)
    with pytest.raises(ModelNotAllowedError, match="not on the allowlist"):
        check_model("sale.order", allowed)


def test_model_denylist_covers_expected_categories() -> None:
    # Sanity: the five buckets are all represented.
    assert "res.users" in MODEL_DENYLIST
    assert "ir.config_parameter" in MODEL_DENYLIST
    assert "ir.actions.server" in MODEL_DENYLIST
    assert "mail.template" in MODEL_DENYLIST
    assert "ir.attachment" in MODEL_DENYLIST


def test_denylist_contents_are_locked_in() -> None:
    """The denylist is the single most security-critical constant in the
    codebase. This test pins it to the expected contents so a refactor
    that accidentally trims a sensitive entry trips CI before merge.

    Adding new entries: update both the denylist and this test, with
    a comment in the commit explaining why the new model is dangerous.
    Removing entries: should be very rare and require explicit review.
    """
    required = {
        # Auth / user / group
        "res.users",
        "res.users.log",
        "res.users.apikeys",
        "res.users.apikeys.description",
        "res.users.identitycheck",
        "res.groups",
        "auth_totp.device",
        "auth_oauth.provider",
        "auth_signup.reset.password",
        # System config + ACL
        "ir.config_parameter",
        "ir.model.access",
        "ir.rule",
        # Stored executable content
        "ir.actions.server",
        "ir.actions.client",
        "ir.ui.view",
        "mail.template",
        # Scheduler / module / log internals
        "ir.cron",
        "ir.module.module",
        "ir.logging",
        "ir.sequence",
        # Model metadata
        "ir.model",
        "ir.model.fields",
        "ir.model.data",
        # Raw attachments
        "ir.attachment",
        # Import/export infra
        "base_import.import",
        "base_import.mapping",
    }
    missing = required - MODEL_DENYLIST
    assert not missing, f"MODEL_DENYLIST is missing: {sorted(missing)}"
