"""Unit tests for :mod:`odoo_mcp._scan_heuristics`.

The heuristics module is the security-meaningful part of the v0.10 scan
feature — every classification rule lives there, and the CLI is just a
thin wrapper around it. We pin each rule (positive + negative) plus the
Dutch / Flemish keyword coverage that BE klanten depend on.
"""

from __future__ import annotations

from odoo_mcp._scan_heuristics import (
    Sensitivity,
    classify_field,
    is_custom_field_name,
    is_studio_field_name,
)


def _classify(
    name: str,
    *,
    ftype: str = "char",
    help_text: str = "",
    blocked: bool = False,
    gated: bool = False,
) -> Sensitivity:
    return classify_field(
        "hr.employee",
        name,
        {"type": ftype, "help": help_text},
        is_blocked=blocked,
        is_gated=gated,
    ).sensitivity


# ---- Custom-name detection -----------------------------------------------


def test_custom_field_name_studio() -> None:
    assert is_custom_field_name("x_studio_foo")
    assert is_studio_field_name("x_studio_foo")


def test_custom_field_name_x_prefix() -> None:
    assert is_custom_field_name("x_klantx_pin")
    assert not is_studio_field_name("x_klantx_pin")


def test_custom_field_name_negative() -> None:
    assert not is_custom_field_name("name")
    assert not is_custom_field_name("email")


# ---- BLOCKED / GATED short-circuits --------------------------------------


def test_blocked_short_circuits() -> None:
    assert _classify("x_my_api_key", blocked=True) is Sensitivity.BLOCKED


def test_gated_short_circuits() -> None:
    assert _classify("x_studio_extra_vat", gated=True) is Sensitivity.GATED


# ---- English keywords -----------------------------------------------------


def test_salary_in_name() -> None:
    assert _classify("x_studio_salary_grade", ftype="many2one") is Sensitivity.LIKELY_SENSITIVE


def test_iban_in_name() -> None:
    assert _classify("x_iban_secondary") is Sensitivity.LIKELY_SENSITIVE


def test_birth_in_name() -> None:
    assert _classify("x_birth_date", ftype="date") is Sensitivity.LIKELY_SENSITIVE


def test_passport_in_name() -> None:
    assert _classify("x_passport_no") is Sensitivity.LIKELY_SENSITIVE


def test_private_email() -> None:
    assert _classify("x_private_email") is Sensitivity.LIKELY_SENSITIVE


# ---- Dutch / Flemish keywords (BE klanten) -------------------------------


def test_loon_dutch_wage() -> None:
    assert _classify("x_loon_groep", ftype="selection") is Sensitivity.LIKELY_SENSITIVE


def test_geboorte_dutch_birth() -> None:
    assert _classify("x_geboortedatum", ftype="date") is Sensitivity.LIKELY_SENSITIVE


def test_rijksregister_be_national_id() -> None:
    assert _classify("x_rijksregister_nr") is Sensitivity.LIKELY_SENSITIVE


def test_geslacht_dutch_gender() -> None:
    assert _classify("x_geslacht") is Sensitivity.LIKELY_SENSITIVE


def test_burgerlijk_dutch_marital() -> None:
    assert _classify("x_burgerlijke_staat") is Sensitivity.LIKELY_SENSITIVE


def test_btw_be_vat() -> None:
    assert _classify("x_btw_secondary") is Sensitivity.LIKELY_SENSITIVE


# ---- Help-text matches ---------------------------------------------------


def test_help_text_confidential_en() -> None:
    assert (
        _classify("x_klantx_pin", help_text="Internal use only — do not share")
        is Sensitivity.LIKELY_SENSITIVE
    )


def test_help_text_vertrouwelijk_nl() -> None:
    assert (
        _classify("x_some_field", help_text="Vertrouwelijk veld voor HR")
        is Sensitivity.LIKELY_SENSITIVE
    )


def test_help_text_persoonlijk_nl() -> None:
    assert (
        _classify("x_field", help_text="Persoonlijk veld, niet delen")
        is Sensitivity.LIKELY_SENSITIVE
    )


# ---- Financial type + keyword combo --------------------------------------


def test_monetary_amount_field() -> None:
    assert _classify("x_studio_amount_extra", ftype="monetary") is Sensitivity.LIKELY_FINANCIAL


def test_float_rate_field() -> None:
    assert _classify("x_studio_billing_rate", ftype="float") is Sensitivity.LIKELY_FINANCIAL


def test_float_without_financial_keyword_is_uncertain() -> None:
    assert _classify("x_studio_priority_pct", ftype="float") is Sensitivity.UNCERTAIN


# ---- Binary stripping ----------------------------------------------------


def test_binary_field_flagged() -> None:
    assert _classify("x_attachment_blob", ftype="binary") is Sensitivity.BINARY_AUTO_STRIPPED


# ---- Default UNCERTAIN ---------------------------------------------------


def test_uncertain_default() -> None:
    assert _classify("x_studio_color_choice", ftype="selection") is Sensitivity.UNCERTAIN


def test_uncertain_default_char() -> None:
    assert _classify("x_klantx_label") is Sensitivity.UNCERTAIN


# ---- False-positive guards -----------------------------------------------


def test_no_match_for_internal_substring() -> None:
    # "internalisation" should NOT trip "intern" — the boundary regex prevents it.
    assert _classify("x_internalisation_score", ftype="float") is Sensitivity.UNCERTAIN


def test_no_match_for_keynote() -> None:
    # "keynote" contains "key" but the keyword set requires word/underscore
    # boundary — these are tested via the always-redacted regex elsewhere.
    assert _classify("x_keynote_color") is Sensitivity.UNCERTAIN


# ---- Binary fields that are ALSO sensitive -------------------------------
#
# Regression guard for the ordering contract in the module docstring
# ("most-severe wins"). Binary stripping is ergonomics, not security: it is
# bypassed by ``include_binary=true`` and never reaches the generated
# redaction policy, so a Binary field with a sensitive name must be reported
# as LIKELY_SENSITIVE rather than BINARY_AUTO_STRIPPED.
#
# These names matter for BE klanten specifically: the built-in
# ``_ALWAYS_REDACTED_PATTERNS`` cover ``salary`` / ``payroll`` in English
# only, so the Dutch / French equivalents below reach the operator's config
# ONLY via this heuristic.


def test_binary_with_sensitive_name_is_reported_sensitive() -> None:
    assert _classify("x_studio_loonfiche", ftype="binary") is Sensitivity.LIKELY_SENSITIVE


def test_binary_with_sensitive_name_iban() -> None:
    assert _classify("x_studio_iban_bewijs", ftype="binary") is Sensitivity.LIKELY_SENSITIVE


def test_binary_with_sensitive_help_text_is_reported_sensitive() -> None:
    assert (
        _classify("x_studio_attest", ftype="binary", help_text="Strikt vertrouwelijk")
        is Sensitivity.LIKELY_SENSITIVE
    )


def test_binary_verdict_reason_still_mentions_stripping() -> None:
    """The Binary fact is preserved in the reason, not lost to the upgrade."""
    verdict = classify_field(
        "hr.employee",
        "x_studio_loonfiche",
        {"type": "binary", "help": ""},
        is_blocked=False,
        is_gated=False,
    )
    assert "loon" in verdict.reason
    assert "include_binary" in verdict.reason


def test_plain_binary_still_informational() -> None:
    """No sensitivity signal -> unchanged behaviour (no false-positive inflation)."""
    assert _classify("x_studio_photo", ftype="binary") is Sensitivity.BINARY_AUTO_STRIPPED
    assert _classify("x_attachment_blob", ftype="binary") is Sensitivity.BINARY_AUTO_STRIPPED


def test_blocked_and_gated_still_outrank_binary() -> None:
    assert _classify("x_studio_loonfiche", ftype="binary", blocked=True) is Sensitivity.BLOCKED
    assert _classify("x_studio_loonfiche", ftype="binary", gated=True) is Sensitivity.GATED
