"""Tests for field redaction, binary stripping, and the read/write policies."""

from __future__ import annotations

import pytest

from odoo_mcp.errors import ConfigError, FieldPolicyError
from odoo_mcp.security.fields import (
    compile_extra_patterns,
    is_always_redacted,
    is_always_redacted_with_extra,
    is_default_hidden,
    redact_fields_get,
    redact_response,
    validate_aggregate_fields,
    validate_groupby,
    validate_requested_fields,
    validate_write_values,
)

PARTNER_FIELDS = frozenset(
    {"id", "name", "email", "vat", "bank_ids", "company_registry", "image_1920", "active"}
)
EMPLOYEE_FIELDS = frozenset({"id", "name", "ssnid", "private_email", "api_key"})


# --- always-redacted pattern matching ----------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "password",
        "password_crypt",
        "api_key",
        "new_password",
        "refresh_token",
        "access_token",
        "some_module_api_key",
        "shopify_password",
        "stripe_secret",
        "webhook_token",
    ],
)
def test_always_redacted_matches(field: str) -> None:
    assert is_always_redacted(field)


@pytest.mark.parametrize(
    "field",
    ["name", "email", "note", "keynote", "token_count", "password_holder_name"],
)
def test_always_redacted_does_not_false_positive(field: str) -> None:
    # We only match full field names against the regex, not substrings,
    # so normal business fields with "token" or "password" in them as
    # substring don't get caught.
    assert not is_always_redacted(field), f"false positive on {field!r}"


def test_default_hidden_by_model() -> None:
    assert is_default_hidden("res.partner", "vat")
    assert is_default_hidden("hr.employee", "ssnid")
    assert not is_default_hidden("crm.lead", "vat")


def test_default_hidden_respects_instance_overrides() -> None:
    # Override: add 'ref' as hidden on res.partner AND drop 'vat' for this instance.
    overrides = {"res.partner": frozenset({"ref"})}
    assert is_default_hidden("res.partner", "ref", instance_overrides=overrides)
    assert not is_default_hidden("res.partner", "vat", instance_overrides=overrides)
    # hr.employee not in the overrides map -> global default still applies.
    assert is_default_hidden("hr.employee", "ssnid", instance_overrides=overrides)


def test_default_hidden_empty_override_unhides_everything_for_model() -> None:
    overrides: dict[str, frozenset[str]] = {"res.partner": frozenset()}
    assert not is_default_hidden("res.partner", "vat", instance_overrides=overrides)


def test_validate_requested_fields_uses_instance_overrides() -> None:
    overrides = {"res.partner": frozenset()}
    # Without opt-in, 'vat' would normally be blocked — but this instance
    # declared no hidden fields on res.partner, so the read is allowed.
    out = validate_requested_fields(
        "res.partner",
        ["name", "vat"],
        PARTNER_FIELDS,
        allow_sensitive=frozenset(),
        instance_overrides=overrides,
    )
    assert out == ["name", "vat"]


def test_redact_response_respects_instance_override() -> None:
    overrides = {"res.partner": frozenset()}
    records = [{"id": 1, "name": "Acme", "vat": "BE1234"}]
    out = redact_response(
        "res.partner",
        records,
        field_types={"id": "integer", "name": "char", "vat": "char"},
        allow_sensitive=frozenset(),
        include_binary=False,
        instance_overrides=overrides,
    )
    assert out == [{"id": 1, "name": "Acme", "vat": "BE1234"}]


def test_redact_fields_get_respects_instance_override() -> None:
    fg = {
        "name": {"type": "char"},
        "vat": {"type": "char"},
        "ref": {"type": "char"},
    }
    overrides = {"res.partner": frozenset({"ref"})}
    out = redact_fields_get("res.partner", fg, instance_overrides=overrides)
    assert "_sensitive" not in out["vat"]
    assert out["ref"].get("_sensitive") is True


# --- validate_requested_fields ---------------------------------------------


def test_requires_explicit_field_list() -> None:
    with pytest.raises(FieldPolicyError, match="Explicit field"):
        validate_requested_fields("res.partner", [], PARTNER_FIELDS, allow_sensitive=frozenset())


def test_rejects_unknown_field() -> None:
    with pytest.raises(FieldPolicyError, match="does not exist"):
        validate_requested_fields(
            "res.partner", ["name", "fictional"], PARTNER_FIELDS, allow_sensitive=frozenset()
        )


def test_rejects_dotted_field() -> None:
    with pytest.raises(FieldPolicyError, match="Dotted"):
        validate_requested_fields(
            "res.partner", ["create_uid.login"], PARTNER_FIELDS, allow_sensitive=frozenset()
        )


def test_always_redacted_field_rejected_even_with_allow_sensitive() -> None:
    with pytest.raises(FieldPolicyError, match="permanently redacted"):
        validate_requested_fields(
            "hr.employee",
            ["api_key"],
            EMPLOYEE_FIELDS,
            allow_sensitive=frozenset({"api_key"}),
        )


def test_default_hidden_field_rejected_without_opt_in() -> None:
    with pytest.raises(FieldPolicyError, match="sensitive"):
        validate_requested_fields(
            "res.partner", ["name", "vat"], PARTNER_FIELDS, allow_sensitive=frozenset()
        )


def test_default_hidden_field_allowed_with_opt_in() -> None:
    out = validate_requested_fields(
        "res.partner",
        ["name", "vat"],
        PARTNER_FIELDS,
        allow_sensitive=frozenset({"vat"}),
    )
    assert out == ["name", "vat"]


# --- validate_write_values --------------------------------------------------


def test_write_values_must_be_non_empty() -> None:
    with pytest.raises(FieldPolicyError, match="non-empty"):
        validate_write_values("res.partner", {}, PARTNER_FIELDS)


def test_write_rejects_unknown_field() -> None:
    with pytest.raises(FieldPolicyError, match="does not exist"):
        validate_write_values("res.partner", {"fictional": 1}, PARTNER_FIELDS)


def test_write_rejects_always_redacted() -> None:
    with pytest.raises(FieldPolicyError, match="protected"):
        validate_write_values("hr.employee", {"name": "x", "api_key": "zzzz"}, EMPLOYEE_FIELDS)


def test_write_allows_default_hidden_fields() -> None:
    # You can legitimately set vat; you just can't read it back blindly.
    out = validate_write_values("res.partner", {"name": "Acme", "vat": "BE1234"}, PARTNER_FIELDS)
    assert out == {"name": "Acme", "vat": "BE1234"}


# --- redact_response --------------------------------------------------------


def test_response_redaction_drops_sensitive_by_default() -> None:
    records = [{"id": 1, "name": "Acme", "vat": "BE1234", "email": "a@b.c"}]
    out = redact_response(
        "res.partner",
        records,
        field_types={"id": "integer", "name": "char", "vat": "char", "email": "char"},
        allow_sensitive=frozenset(),
        include_binary=False,
    )
    assert out == [{"id": 1, "name": "Acme", "email": "a@b.c"}]


def test_response_redaction_returns_sensitive_when_unlocked() -> None:
    records = [{"id": 1, "name": "Acme", "vat": "BE1234"}]
    out = redact_response(
        "res.partner",
        records,
        field_types={"id": "integer", "name": "char", "vat": "char"},
        allow_sensitive=frozenset({"vat"}),
        include_binary=False,
    )
    assert out == [{"id": 1, "name": "Acme", "vat": "BE1234"}]


def test_response_strips_binary_field() -> None:
    records = [{"id": 1, "name": "Acme", "image_1920": "A" * 4000}]
    out = redact_response(
        "res.partner",
        records,
        field_types={"id": "integer", "name": "char", "image_1920": "binary"},
        allow_sensitive=frozenset(),
        include_binary=False,
    )
    assert out[0]["image_1920"].startswith("<binary:")
    assert "3000" in out[0]["image_1920"]  # 4000 base64 -> ~3000 bytes


def test_response_includes_binary_when_opted_in() -> None:
    records = [{"id": 1, "image_1920": "ABCD"}]
    out = redact_response(
        "res.partner",
        records,
        field_types={"id": "integer", "image_1920": "binary"},
        allow_sensitive=frozenset(),
        include_binary=True,
    )
    assert out[0]["image_1920"] == "ABCD"


def test_response_always_drops_always_redacted_even_if_odoo_returns_it() -> None:
    # Defense in depth: if the remote model somehow returns a password field,
    # the redactor drops it regardless of what the allow_sensitive set says.
    records = [{"id": 1, "name": "X", "password": "HASH"}]
    out = redact_response(
        "res.partner",
        records,
        field_types={"id": "integer", "name": "char", "password": "char"},
        allow_sensitive=frozenset({"password"}),
        include_binary=False,
    )
    assert "password" not in out[0]


# --- redact_fields_get ------------------------------------------------------


def test_redact_fields_get_filters_always_redacted() -> None:
    fg = {
        "name": {"type": "char"},
        "password": {"type": "char"},
        "vat": {"type": "char"},
    }
    out = redact_fields_get("res.partner", fg)
    assert "password" not in out
    assert "vat" in out
    assert out["vat"].get("_sensitive") is True


# --- validate_aggregate_fields (read_group fields arg) -----------------------


LEAD_FIELDS = frozenset(
    {"id", "name", "stage_id", "user_id", "expected_revenue", "create_date", "api_key"}
)


def test_validate_aggregate_fields_accepts_plain_and_typed() -> None:
    out = validate_aggregate_fields(
        "crm.lead",
        ["expected_revenue:sum", "id:count", "stage_id"],
        LEAD_FIELDS,
        allow_sensitive=frozenset(),
    )
    assert out == ["expected_revenue:sum", "id:count", "stage_id"]


@pytest.mark.parametrize(
    "spec",
    [
        "expected_revenue:median",  # not in agg whitelist
        "expected_revenue:SUM",  # case-sensitive
        "expected_revenue:sum:extra",  # too many colons
        "alias:sum(expected_revenue)",  # alias syntax blocked
        ":sum",  # empty field
        "",
    ],
)
def test_validate_aggregate_fields_rejects_bad_syntax(spec: str) -> None:
    with pytest.raises(FieldPolicyError):
        validate_aggregate_fields("crm.lead", [spec], LEAD_FIELDS, allow_sensitive=frozenset())


def test_validate_aggregate_fields_rejects_dotted() -> None:
    with pytest.raises(FieldPolicyError):
        validate_aggregate_fields(
            "crm.lead",
            ["user_id.login:count"],
            LEAD_FIELDS,
            allow_sensitive=frozenset(),
        )


def test_validate_aggregate_fields_rejects_unknown_field() -> None:
    with pytest.raises(FieldPolicyError):
        validate_aggregate_fields(
            "crm.lead",
            ["bogus_field:sum"],
            LEAD_FIELDS,
            allow_sensitive=frozenset(),
        )


def test_validate_aggregate_fields_rejects_always_redacted() -> None:
    with pytest.raises(FieldPolicyError):
        validate_aggregate_fields(
            "crm.lead",
            ["api_key:count"],
            LEAD_FIELDS,
            allow_sensitive=frozenset({"api_key"}),  # even with opt-in
        )


def test_validate_aggregate_fields_requires_optin_for_sensitive() -> None:
    partner_fields = frozenset({"id", "name", "vat"})
    with pytest.raises(FieldPolicyError):
        validate_aggregate_fields(
            "res.partner", ["vat:count"], partner_fields, allow_sensitive=frozenset()
        )
    # With opt-in: accepted.
    out = validate_aggregate_fields(
        "res.partner",
        ["vat:count"],
        partner_fields,
        allow_sensitive=frozenset({"vat"}),
    )
    assert out == ["vat:count"]


def test_validate_aggregate_fields_rejects_empty_list() -> None:
    with pytest.raises(FieldPolicyError):
        validate_aggregate_fields("crm.lead", [], LEAD_FIELDS, allow_sensitive=frozenset())


# --- validate_groupby (read_group groupby arg) -------------------------------


def test_validate_groupby_accepts_plain_and_time_bucket() -> None:
    out = validate_groupby(
        "crm.lead",
        ["stage_id", "create_date:month"],
        LEAD_FIELDS,
        allow_sensitive=frozenset(),
    )
    assert out == ["stage_id", "create_date:month"]


@pytest.mark.parametrize(
    "spec",
    [
        "create_date:decade",  # not in granularity whitelist
        "create_date:DAY",  # case-sensitive
        "create_date:day:extra",  # too many colons
        ":month",  # empty field
        "",
    ],
)
def test_validate_groupby_rejects_bad_syntax(spec: str) -> None:
    with pytest.raises(FieldPolicyError):
        validate_groupby("crm.lead", [spec], LEAD_FIELDS, allow_sensitive=frozenset())


def test_validate_groupby_rejects_dotted() -> None:
    with pytest.raises(FieldPolicyError):
        validate_groupby(
            "crm.lead",
            ["user_id.login"],
            LEAD_FIELDS,
            allow_sensitive=frozenset(),
        )


def test_validate_groupby_rejects_unknown_field() -> None:
    with pytest.raises(FieldPolicyError):
        validate_groupby("crm.lead", ["bogus"], LEAD_FIELDS, allow_sensitive=frozenset())


def test_validate_groupby_rejects_always_redacted() -> None:
    with pytest.raises(FieldPolicyError):
        validate_groupby(
            "crm.lead",
            ["api_key"],
            LEAD_FIELDS,
            allow_sensitive=frozenset({"api_key"}),  # even with opt-in
        )


def test_validate_groupby_requires_optin_for_sensitive() -> None:
    partner_fields = frozenset({"id", "name", "vat"})
    with pytest.raises(FieldPolicyError):
        validate_groupby("res.partner", ["vat"], partner_fields, allow_sensitive=frozenset())
    out = validate_groupby(
        "res.partner", ["vat"], partner_fields, allow_sensitive=frozenset({"vat"})
    )
    assert out == ["vat"]


def test_validate_groupby_rejects_empty_list() -> None:
    with pytest.raises(FieldPolicyError):
        validate_groupby("crm.lead", [], LEAD_FIELDS, allow_sensitive=frozenset())


def test_validate_groupby_caps_dimensions() -> None:
    # 5 dimensions is over the cap of 4.
    with pytest.raises(FieldPolicyError):
        validate_groupby(
            "crm.lead",
            ["id", "name", "stage_id", "user_id", "create_date:day"],
            LEAD_FIELDS,
            allow_sensitive=frozenset(),
        )


# --- broader built-in patterns + per-instance custom patterns ---------------


@pytest.mark.parametrize(
    "field",
    [
        "employee_salary",
        "gross_compensation",
        "yearly_payroll",
        "quarterly_bonus",
        "commission_amount",
        "private_key",
        "api_passphrase",
        "db_credentials",
        "nda_text",
        "confidential",
    ],
)
def test_broader_builtin_patterns_redacted(field: str) -> None:
    assert is_always_redacted(field)


def test_custom_pattern_redacts() -> None:
    extra = compile_extra_patterns([r"my_module\.\w+_blob"])
    assert is_always_redacted_with_extra("my_module.foo_blob", extra)
    assert not is_always_redacted_with_extra("other_module.foo_blob", extra)
    # And without the custom patterns, the same field is not blocked.
    assert not is_always_redacted_with_extra("my_module.foo_blob", ())


def test_invalid_regex_raises_clear_error() -> None:
    # The config-loader path surfaces this as ConfigError; the runtime helper
    # surfaces it as FieldPolicyError. Both messages must mention the pattern.
    from odoo_mcp.config import (
        Defaults,  # noqa: PLC0415
        _parse_one_instance,  # noqa: PLC0415
    )

    with pytest.raises(ConfigError, match=r"\["):
        _parse_one_instance(
            "broken",
            {
                "url": "https://x.example.com",
                "database": "db",
                "credentials_env_prefix": "ODOO_MCP_BROKEN",
                "custom_sensitive_field_patterns": ["["],
            },
            Defaults(),
        )

    with pytest.raises(FieldPolicyError, match=r"\["):
        compile_extra_patterns(["["])


def test_custom_pattern_blocks_even_with_allow_sensitive() -> None:
    extra = compile_extra_patterns([r"client_data"])
    fields = frozenset({"id", "name", "client_data"})
    # The "always redacted" semantics apply to custom patterns too — even if
    # the caller opts in, the field stays blocked.
    with pytest.raises(FieldPolicyError, match="permanently redacted"):
        validate_requested_fields(
            "res.partner",
            ["client_data"],
            fields,
            allow_sensitive=frozenset({"client_data"}),
            extra_redacted=extra,
        )
    # And on responses: the value is dropped even if the caller listed it as
    # opted-in.
    out = redact_response(
        "res.partner",
        [{"id": 1, "name": "Acme", "client_data": "secret"}],
        field_types={"id": "integer", "name": "char", "client_data": "char"},
        allow_sensitive=frozenset({"client_data"}),
        include_binary=False,
        extra_redacted=extra,
    )
    assert "client_data" not in out[0]


def test_redact_response_drops_field_with_no_type_info() -> None:
    """Defense-in-depth: fields not in field_types are dropped.

    A custom-module field that's missing from ``fields_get`` could be a
    binary blob; we'd rather drop it than risk passing an un-stripped
    base64 payload through. The dispatcher pre-validates the requested
    field list against ``fields_get`` so this branch never fires for
    normal callers — but a returned record carrying an unexpected key
    (Odoo edge case, custom override) is now safely silenced.
    """
    out = redact_response(
        "res.partner",
        [{"id": 1, "name": "Acme", "mystery_field": "blob"}],
        field_types={"id": "integer", "name": "char"},
        allow_sensitive=frozenset(),
        include_binary=False,
    )
    assert out == [{"id": 1, "name": "Acme"}]
    assert "mystery_field" not in out[0]
