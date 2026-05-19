"""Tests for the model and operation allowlist."""

from __future__ import annotations

import pytest

from odoo_mcp.errors import ModelNotAllowedError, OperationNotAllowedError
from odoo_mcp.security.allowlist import (
    ALLOWLIST_WILDCARD,
    MODEL_DENYLIST,
    MODEL_WRITE_BLOCKLIST,
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
        "res.users.apikeys.show",
        "res.users.identitycheck",
        "res.users.deletion",
        "res.users.settings",
        "res.users.settings.volumes",
        "res.users.role",
        "res.users.role.line",
        "res.groups",
        "auth_totp.device",
        "auth_oauth.provider",
        "auth_signup.reset.password",
        "auth.oauth.provider",
        "auth.passkey.key",
        "auth.totp.rate.limit.log",
        # System config + ACL
        "ir.config_parameter",
        "ir.model.access",
        "ir.rule",
        "ir.default",
        "ir.filters",
        # Stored executable content
        "ir.actions.server",
        "ir.actions.client",
        "ir.actions.act_url",
        "ir.actions.todo",
        "ir.embedded.actions",
        "ir.ui.view",
        "ir.asset",
        "mail.template",
        "base.automation",
        "base.automation.lint",
        "base.automation.line.test",
        "mcp.access.profile",
        # Mail server credentials and gateway/credential storage
        "ir.mail_server",
        "fetchmail.server",
        "mail.gateway.allowed",
        "google.gmail.mixin",
        "microsoft.outlook.mixin",
        "google.service",
        "microsoft.service",
        "google.calendar.sync",
        "microsoft.calendar.sync",
        # IAP account tokens
        "iap.account",
        "iap.service",
        # Payment provider tokens / transactions
        "payment.token",
        "payment.transaction",
        "payment.provider",
        "payment.method",
        # Scheduler / module / log internals
        "ir.cron",
        "ir.cron.progress",
        "ir.cron.trigger",
        "ir.module.module",
        "ir.module.category",
        "ir.logging",
        "ir.profile",
        "ir.sequence",
        # Real-time bus / presence
        "bus.bus",
        "bus.presence",
        # Model metadata
        "ir.model",
        "ir.model.fields",
        "ir.model.fields.selection",
        "ir.model.constraint",
        "ir.model.relation",
        "ir.model.inherit",
        "ir.model.data",
        # Raw attachments
        "ir.attachment",
        # Import/export infra
        "base_import.import",
        "base_import.mapping",
        "ir.exports",
        "ir.exports.line",
    }
    missing = required - MODEL_DENYLIST
    assert not missing, f"MODEL_DENYLIST is missing: {sorted(missing)}"


def test_rights_modification_models_all_denied() -> None:
    """Every known rights-modification vector is denied in open mode.

    Defense in depth: even if someone forgets the denylist intent, this
    test pins the specific models that grant or revoke privileges in
    Odoo. If a future refactor accidentally drops one of these, this
    test fails loudly.

    The set is grouped by escalation type, with a comment per group so
    a reviewer adding a new entry understands the category.
    """
    allowed = frozenset({ALLOWLIST_WILDCARD})

    rights_models: dict[str, list[str]] = {
        "direct user / group membership": [
            "res.users",
            "res.users.log",
            "res.groups",
            "res.users.role",
            "res.users.role.line",
        ],
        "API keys + auth tokens": [
            "res.users.apikeys",
            "res.users.apikeys.description",
            "res.users.apikeys.show",
            "auth_totp.device",
            "auth.oauth.provider",
            "auth.passkey.key",
        ],
        "ACL + record rules + defaults": [
            "ir.model.access",
            "ir.rule",
            "ir.default",
            "ir.filters",
        ],
        "executable content / automation": [
            "ir.actions.server",
            "ir.actions.client",
            "ir.actions.todo",
            "ir.embedded.actions",
            "base.automation",
            "ir.cron",
        ],
        "model + module metadata": [
            "ir.model",
            "ir.model.fields",
            "ir.module.module",
        ],
        "MCP own controls (when companion addon is installed)": [
            "mcp.access.profile",
        ],
    }
    for category, models in rights_models.items():
        for model in models:
            with pytest.raises(ModelNotAllowedError, match="denylist"):
                check_model(model, allowed)
            # Reading the error message lets us confirm the rejection
            # cites the denylist specifically, not a generic "unknown
            # model" / "name shape" / etc.
            try:
                check_model(model, allowed)
            except ModelNotAllowedError as exc:
                assert "denylist" in str(exc), (
                    f"{category}/{model}: error did not mention denylist: {exc}"
                )


def test_write_blocklist_contents_are_locked_in() -> None:
    """Pin the write-blocklist contents.

    Like the denylist, this is a hard safety invariant. Adding entries
    tightens; removing should be very rare and reviewed. The blocklist
    closes the side-door that opens when ``mail.message`` and friends
    are made readable by default (v0.13.1 F1) — without it, a write
    path could be used to send messages or post log notes via the MCP.
    It also covers the wider collaboration layer — activities and
    Discuss channels — so generic ``odoo_create`` / ``odoo_write`` calls
    cannot act as the user there either.
    """
    required = {
        "mail.message",
        "mail.followers",
        "mail.notification",
        "mail.activity",
        "discuss.channel",
        "discuss.channel.member",
        "mail.channel",
        "mail.channel.member",
    }
    missing = required - MODEL_WRITE_BLOCKLIST
    assert not missing, f"MODEL_WRITE_BLOCKLIST is missing: {sorted(missing)}"
