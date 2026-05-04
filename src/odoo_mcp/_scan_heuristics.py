"""Heuristics for classifying custom Odoo fields by sensitivity.

Used by :mod:`odoo_mcp.scan_cli`. Kept in its own module so the rules are
unit-testable in isolation without spinning up the whole MCP app.

Classification levels (most-severe wins):

* ``BLOCKED`` — already covered by ``_ALWAYS_REDACTED_PATTERNS`` in
  :mod:`odoo_mcp.security.fields`. The operator does NOT need to add these
  to the TOML — they're hard-coded. The scan reports them so the consultant
  knows the field is on the field surface but already protected.
* ``GATED`` — already covered by ``_DEFAULT_HIDDEN`` for the model. Same
  story: visible only when the caller opts in. No TOML override needed.
* ``LIKELY_SENSITIVE`` — name or help-text matches a financial / PII /
  confidentiality keyword. The scan suggests adding it to the
  per-instance ``custom_sensitive_field_patterns`` or
  ``sensitive_fields[<model>]`` block.
* ``LIKELY_FINANCIAL`` — Float / Monetary type AND a financial keyword.
  Same suggestion as LIKELY_SENSITIVE.
* ``BINARY_AUTO_STRIPPED`` — Binary field. Stripped by default for
  ergonomics; informational only.
* ``UNCERTAIN`` — nothing matched. Operator should review manually.

Belgian / Dutch keyword coverage is deliberate — deltix klanten are mostly
Belgian and use mixed Dutch / French Studio field names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class Sensitivity(StrEnum):
    BLOCKED = "BLOCKED"
    GATED = "GATED"
    LIKELY_SENSITIVE = "LIKELY_SENSITIVE"
    LIKELY_FINANCIAL = "LIKELY_FINANCIAL"
    BINARY_AUTO_STRIPPED = "BINARY_AUTO_STRIPPED"
    UNCERTAIN = "UNCERTAIN"


# Keywords that signal personally-identifying or confidential content. Each
# entry is a substring matched case-insensitively against the field's
# ``name`` (and, separately, against the help text). Belgian / Dutch /
# French equivalents are intentionally first-class — the consultant base
# is mostly BE klanten with mixed-language Studio field names.
_NAME_SENSITIVE_KEYWORDS: Final[tuple[str, ...]] = (
    # Compensation
    "salary",
    "salar",
    "wage",
    "loon",
    "compensation",
    "payroll",
    "bonus",
    "commission",
    "remuneration",
    # Bank / payment
    "iban",
    "bic",
    "swift",
    "bank_account",
    "bank_acc",
    "bankaccount",
    "rekening",  # NL: account
    "account_number",
    # Tax / national IDs
    "vat",
    "tva",
    "btw",
    "tax_id",
    "passport",
    "ssn",
    "sin",
    "nrn",  # BE: numéro de registre national
    "nin",
    "rrn",  # BE: rijksregisternummer
    "rijksregister",
    "nationaal_nr",
    "national_id",
    "national_register",
    "registr",
    # Personal life
    "geboorte",  # NL: birth
    "birth",
    "birthday",
    "dob",
    "private",
    "personal",
    "persoonlijk",  # NL: personal
    "home_",
    "_home",
    "gsm",
    "mobile",
    "telefoon",
    "phone_private",
    "private_phone",
    "private_email",
    "email_private",
    "gender",
    "geslacht",  # NL: gender
    "marital",
    "burgerlijk",  # NL: civil/marital
    "etat_civil",
    "religion",
    # Confidentiality flags
    "confidential",
    "vertrouwelijk",  # NL: confidential
    "secret",
    # ``intern`` / ``internal`` deliberately omitted as bare keywords —
    # too broad (matches "internationalisation", "internship", etc.).
    # Help-text matching catches the actual sensitive uses.
)

# Help-text keywords. Same Dutch/Flemish-aware coverage. Anchored on word
# boundaries to reduce false positives from substrings inside common English
# words like "international".
_HELP_SENSITIVE_KEYWORDS: Final[tuple[str, ...]] = (
    "private",
    "confidential",
    "personal",
    "internal use only",
    "intern gebruik",
    "vertrouwelijk",
    "persoonlijk",
    "alleen intern",
    "do not share",
    "niet delen",
    "rgpd",
    "gdpr",
    "salary",
    "loon",
    "salaire",
    "compensation",
    "bank account",
    "national register",
    "rijksregister",
    "geboortedatum",
)

# Financial keyword set — narrower than the PII set above. Used only in
# combination with a numeric field type.
_FINANCIAL_KEYWORDS: Final[tuple[str, ...]] = (
    "amount",
    "bedrag",  # NL: amount
    "price",
    "prijs",  # NL: price
    "cost",
    "kost",  # NL: cost
    "fee",
    "rate",
    "margin",
    "marge",  # NL/FR: margin
    "tarif",
    "revenue",
    "omzet",  # NL: revenue
    "turnover",
)

# Compiled name-keyword regex. The leading boundary (`^|_|\b`) prevents
# "internalisation" matching "intern", but the trailing side is
# deliberately loose — Dutch compound nouns like "geboortedatum" or
# inflections like "burgerlijke" stick a suffix straight onto the
# keyword. We accept any continuation as long as the keyword starts at a
# word boundary. False-positive cost is low (the flagged field still
# only ends up in the suggested-config snippet, which the consultant
# reviews).
_NAME_KEYWORD_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|_|\b)(?:" + "|".join(re.escape(k) for k in _NAME_SENSITIVE_KEYWORDS) + r")",
    re.IGNORECASE,
)
_FINANCIAL_KEYWORD_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|_|\b)(?:" + "|".join(re.escape(k) for k in _FINANCIAL_KEYWORDS) + r")(?:_|\b|$)",
    re.IGNORECASE,
)

_NUMERIC_TYPES: Final[frozenset[str]] = frozenset({"float", "monetary"})


@dataclass(slots=True, frozen=True)
class FieldVerdict:
    """Outcome of classifying a single (model, field) pair."""

    sensitivity: Sensitivity
    reason: str


def is_custom_field_name(name: str) -> bool:
    """True if *name* is a Studio / manual-custom field by naming convention.

    Odoo Studio prefixes every field it creates with ``x_studio_``.
    Operator-added Python custom modules conventionally use the ``x_``
    prefix as well (the ORM tolerates non-prefixed names but Odoo's own
    advice is to use ``x_``).
    """
    return name.startswith("x_") or name.startswith("x_studio_")


def is_studio_field_name(name: str) -> bool:
    return name.startswith("x_studio_")


def classify_field(
    model: str,
    field_name: str,
    field_meta: dict[str, object],
    *,
    is_blocked: bool,
    is_gated: bool,
) -> FieldVerdict:
    """Classify one custom field.

    *field_meta* is the per-field dict from Odoo's ``fields_get`` — we look
    at ``type`` and ``help`` only. Any other key is ignored. ``is_blocked``
    and ``is_gated`` are passed in by the caller so this module doesn't
    have to import the security regex tables (keeps the dep graph one-way).
    """
    if is_blocked:
        return FieldVerdict(
            Sensitivity.BLOCKED,
            "matched always-redacted pattern (already covered by built-in policy)",
        )
    if is_gated:
        return FieldVerdict(
            Sensitivity.GATED,
            f"already in default-hidden list for {model} (already covered)",
        )

    ftype_raw = field_meta.get("type")
    ftype = ftype_raw.lower() if isinstance(ftype_raw, str) else ""
    help_raw = field_meta.get("help")
    help_text = help_raw if isinstance(help_raw, str) else ""

    # Binary stripping is informational, not a sensitivity flag in itself —
    # but it's worth surfacing so the consultant knows we'll auto-strip.
    if ftype == "binary":
        return FieldVerdict(
            Sensitivity.BINARY_AUTO_STRIPPED,
            "Binary field — auto-stripped from responses unless include_binary=true",
        )

    # Name-based PII match.
    name_match = _NAME_KEYWORD_RE.search(field_name)
    if name_match is not None:
        keyword = name_match.group(0).strip("_")
        return FieldVerdict(
            Sensitivity.LIKELY_SENSITIVE,
            f"name contains sensitive keyword: {keyword!r}",
        )

    # Help-text match (case-insensitive substring is enough — false positives
    # from short English keywords are low because help text is typically a
    # whole sentence).
    if help_text:
        lowered = help_text.lower()
        for kw in _HELP_SENSITIVE_KEYWORDS:
            if kw in lowered:
                return FieldVerdict(
                    Sensitivity.LIKELY_SENSITIVE,
                    f"help text mentions {kw!r}",
                )

    # Numeric + financial keyword.
    if ftype in _NUMERIC_TYPES and _FINANCIAL_KEYWORD_RE.search(field_name):
        return FieldVerdict(
            Sensitivity.LIKELY_FINANCIAL,
            f"{ftype} field with financial keyword in name",
        )

    return FieldVerdict(
        Sensitivity.UNCERTAIN,
        "no heuristic match — review manually",
    )
