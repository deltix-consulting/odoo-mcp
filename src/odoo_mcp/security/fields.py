"""Field-level redaction and binary stripping.

Two categories of redaction:

1. **Always-redacted** — password hashes, API keys, tokens. These are never
   returned, regardless of what the caller asks for. Enforced by regex on
   the field name, not by a fixed list, so new Odoo modules can't sneak a
   ``my_module_api_key`` field past us.

2. **Default-hidden** — sensitive PII (VAT numbers, bank accounts, employee
   SSNs, private phone / email). Returned only if the caller explicitly
   names the field in ``fields`` AND passes ``allow_sensitive_fields=[...]``.
   This forces Claude to opt in per-field, which is something the user can
   review in the tool-call arguments before approving.

Binary fields (as reported by ``fields_get``) are replaced with a placeholder
unless the caller passes ``include_binary=True``. This is pure ergonomics
rather than security, but it matters: base64 blobs blow up the model context
in a hurry.
"""

from __future__ import annotations

import re
from typing import Any, Final

from ..errors import FieldPolicyError

# Regex for fields that are NEVER returned. Note: anchored with fullmatch so
# an incidental "key" in the middle of a word (e.g. "keynote") doesn't trip.
_ALWAYS_REDACTED_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"password", re.IGNORECASE),
    re.compile(r"password_crypt", re.IGNORECASE),
    re.compile(r"new_password", re.IGNORECASE),
    re.compile(r"api_key", re.IGNORECASE),
    re.compile(r".*_api_key", re.IGNORECASE),
    re.compile(r"token", re.IGNORECASE),
    re.compile(r".*_token", re.IGNORECASE),
    re.compile(r"access_token", re.IGNORECASE),
    re.compile(r"refresh_token", re.IGNORECASE),
    re.compile(r".*_secret", re.IGNORECASE),
    re.compile(r".*_password", re.IGNORECASE),
)

# Per-model default-hidden PII. The caller must explicitly opt in to each.
_DEFAULT_HIDDEN: Final[dict[str, frozenset[str]]] = {
    "res.partner": frozenset({"vat", "bank_ids", "company_registry"}),
    "account.payment": frozenset({"partner_bank_id"}),
    "hr.employee": frozenset(
        {
            "ssnid",
            "identification_id",
            "private_email",
            "private_phone",
            "birthday",
            "marital",
            "children",
            "spouse_complete_name",
            "spouse_birthdate",
            "country_of_birth",
            "place_of_birth",
        }
    ),
}

_BINARY_PLACEHOLDER_PREFIX: Final[str] = "<binary:"


def is_always_redacted(field_name: str) -> bool:
    """True if ``field_name`` matches an always-redacted pattern."""
    return any(p.fullmatch(field_name) for p in _ALWAYS_REDACTED_PATTERNS)


def is_default_hidden(model: str, field_name: str) -> bool:
    """True if ``field_name`` is default-hidden on ``model``."""
    return field_name in _DEFAULT_HIDDEN.get(model, frozenset())


def validate_requested_fields(
    model: str,
    requested: list[str],
    known_fields: frozenset[str],
    *,
    allow_sensitive: frozenset[str],
) -> list[str]:
    """Validate the caller's ``fields`` list against all policies.

    Returns the list unchanged if every entry is allowed. Raises
    :class:`FieldPolicyError` on the first violation, so the caller gets a
    clear message about exactly which field is the problem.

    * Always-redacted fields: always rejected, even if they're in
      ``allow_sensitive``.
    * Default-hidden fields: only allowed if in ``allow_sensitive``.
    * Unknown fields (not in ``known_fields``): rejected so the caller
      doesn't get silent empty results from a typo.
    """
    if not isinstance(requested, list) or not requested:
        raise FieldPolicyError(
            "Explicit field list is required — no wildcard reads allowed. "
            "Call odoo_describe_model to see available fields."
        )
    for name in requested:
        if not isinstance(name, str) or not name:
            raise FieldPolicyError(f"Field list must contain non-empty strings, got {name!r}.")
        if "." in name:
            raise FieldPolicyError(
                f"Dotted field {name!r} not allowed — request the relation directly."
            )
        if name not in known_fields:
            raise FieldPolicyError(
                f"Field {name!r} does not exist on model {model!r}."
            )
        if is_always_redacted(name):
            raise FieldPolicyError(
                f"Field {name!r} is permanently redacted and cannot be read."
            )
        if is_default_hidden(model, name) and name not in allow_sensitive:
            raise FieldPolicyError(
                f"Field {name!r} on {model!r} is sensitive and must be explicitly unlocked "
                f"via allow_sensitive_fields=[{name!r}, ...]."
            )
    return list(requested)


def validate_write_values(
    model: str,
    values: dict[str, Any],
    known_fields: frozenset[str],
) -> dict[str, Any]:
    """Validate the ``values`` dict being passed to create/write.

    * Always-redacted fields cannot be written (so the MCP can't be used to
      reset passwords or set API keys).
    * Unknown fields are rejected (typo protection).
    * Default-hidden fields CAN be written (you might legitimately want to
      update a partner's VAT) — but not read back without opting in.
    """
    if not isinstance(values, dict) or not values:
        raise FieldPolicyError("Write values must be a non-empty dict.")
    out: dict[str, Any] = {}
    for name, value in values.items():
        if not isinstance(name, str) or not name:
            raise FieldPolicyError(f"Value key must be a non-empty string, got {name!r}.")
        if "." in name:
            raise FieldPolicyError(f"Dotted field {name!r} not allowed in write values.")
        if name not in known_fields:
            raise FieldPolicyError(
                f"Field {name!r} does not exist on model {model!r}."
            )
        if is_always_redacted(name):
            raise FieldPolicyError(
                f"Field {name!r} is protected and cannot be written via the MCP."
            )
        out[name] = value
    return out


def redact_response(
    model: str,
    records: list[dict[str, Any]],
    field_types: dict[str, str],
    *,
    allow_sensitive: frozenset[str],
    include_binary: bool,
) -> list[dict[str, Any]]:
    """Apply redaction and binary stripping to a batch of records.

    ``field_types`` comes from ``fields_get`` — a mapping of field name to
    its Odoo type string (``"char"``, ``"binary"``, ``"many2one"``, ...).

    We copy each record (rather than mutating) so callers don't accidentally
    retain a reference to the pre-redaction dict.
    """
    out: list[dict[str, Any]] = []
    for rec in records:
        cleaned: dict[str, Any] = {}
        for name, value in rec.items():
            if is_always_redacted(name):
                continue  # drop entirely
            if is_default_hidden(model, name) and name not in allow_sensitive:
                continue  # drop entirely
            if not include_binary and field_types.get(name) == "binary" and value:
                size = _binary_size_hint(value)
                cleaned[name] = f"{_BINARY_PLACEHOLDER_PREFIX}{size} bytes>"
                continue
            cleaned[name] = value
        out.append(cleaned)
    return out


def redact_fields_get(
    model: str, fields_get: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Filter an ``fields_get`` response by the same policy used at read time.

    Used by ``odoo_describe_model`` so the tool never even advertises the
    existence of always-redacted fields, and marks default-hidden ones so
    Claude knows what opt-in is required.
    """
    out: dict[str, dict[str, Any]] = {}
    for name, meta in fields_get.items():
        if is_always_redacted(name):
            continue
        meta_copy = dict(meta)
        if is_default_hidden(model, name):
            meta_copy["_sensitive"] = True
            meta_copy["_note"] = (
                "Default-hidden. Pass allow_sensitive_fields=[...] to unlock per-call."
            )
        out[name] = meta_copy
    return out


def _binary_size_hint(value: Any) -> int:
    """Best-effort size of a base64-encoded binary field."""
    if isinstance(value, bytes):
        return len(value)
    if isinstance(value, str):
        # base64 is ~4/3 the size of the raw bytes
        return (len(value) * 3) // 4
    return 0
