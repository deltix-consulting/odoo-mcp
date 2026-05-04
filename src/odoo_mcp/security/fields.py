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
    # Common HR/finance custom-module fields. Salary and compensation data
    # is highly sensitive, often regulated, and rarely needed for legitimate
    # MCP use cases. Block by default; if a real use case shows up, the
    # operator can carve out an allowlist via custom_sensitive_field_patterns
    # and per-call allow_sensitive_fields (note: built-in patterns can NOT
    # be opted out of — these are always-redacted, not default-hidden).
    re.compile(r".*salary.*", re.IGNORECASE),
    re.compile(r".*compensation.*", re.IGNORECASE),
    re.compile(r".*payroll.*", re.IGNORECASE),
    re.compile(r".*bonus.*", re.IGNORECASE),
    re.compile(r"commission_amount", re.IGNORECASE),
    re.compile(r"nda_text", re.IGNORECASE),
    re.compile(r"confidential", re.IGNORECASE),
    re.compile(r"private_key", re.IGNORECASE),
    re.compile(r"\w+_passphrase", re.IGNORECASE),
    re.compile(r"\w+_credentials", re.IGNORECASE),
)

# Per-model default-hidden PII. The caller must explicitly opt in to each.
#
# The per-model lists below are the result of an evidence-based survey of
# Odoo Community 18.0 (see INDUSTRY_AUDIT.md in the repo root for the
# methodology and full citations). Every entry corresponds to a real
# field on the named model whose contents are personal, financial, or
# otherwise confidential by Odoo convention (e.g. fields gated behind
# ``groups="hr.group_hr_user"`` in the source).
_DEFAULT_HIDDEN: Final[dict[str, frozenset[str]]] = {
    "res.partner": frozenset(
        {
            "vat",
            "bank_ids",
            "company_registry",
            # `comment` is the partner Notes Html field — often used by
            # sales/HR for internal-only notes about the contact.
            "comment",
            # `barcode` is a per-contact identifier — treated as PII.
            "barcode",
        }
    ),
    "res.partner.bank": frozenset({"acc_number"}),
    "account.journal": frozenset({"bank_acc_number"}),
    "account.payment": frozenset({"partner_bank_id", "memo"}),
    "hr.employee": frozenset(
        {
            "ssnid",
            "sinid",
            "identification_id",
            "passport_id",
            "permit_no",
            "visa_no",
            "visa_expire",
            "private_email",
            "private_phone",
            "private_street",
            "private_street2",
            "private_city",
            "private_state_id",
            "private_zip",
            "private_country_id",
            "private_car_plate",
            "birthday",
            "gender",
            "marital",
            "children",
            "spouse_complete_name",
            "spouse_birthdate",
            "country_of_birth",
            "place_of_birth",
            "emergency_contact",
            "emergency_phone",
            "study_field",
            "study_school",
            "km_home_work",
            "bank_account_id",
            "barcode",
        }
    ),
    # hr.contract holds compensation data. The wage fields are not caught by
    # the always-redacted regex (which matches salary/compensation/payroll/
    # bonus but not "wage" alone). Notes can contain HR-internal narrative.
    "hr.contract": frozenset({"wage", "contract_wage", "notes"}),
    # Recruitment is HR-confidential; candidate identity and contact info
    # leaks via these fields if read in bulk.
    "hr.applicant": frozenset(
        {
            "email_from",
            "partner_phone",
            "partner_phone_sanitized",
            "linkedin_profile",
            "refuse_reason_id",
        }
    ),
    "hr.candidate": frozenset(
        {
            "email_from",
            "partner_phone",
            "partner_phone_sanitized",
            "linkedin_profile",
        }
    ),
    # Time-off requests can carry medical / personal context.
    "hr.leave": frozenset({"private_name", "notes"}),
    # Expense Internal Notes — often contain personal context.
    "hr.expense": frozenset({"description"}),
    # Vehicle identifiers are PII (license plate links to a person) and
    # the description / VIN is similarly tracked.
    "fleet.vehicle": frozenset({"license_plate", "vin_sn", "description"}),
    # mail.message is a cross-model side-door: a single message row can
    # reference any res_model (including models NOT on the allowlist), and
    # its `body` / `subject` / email fields can contain anything — HR notes,
    # password resets, private discussions, quoted emails. We hide these by
    # default so a broad read of mail.message gives back only metadata
    # (timestamps, message_type, res_model, res_id, message counts). Opt
    # in per-field via `allow_sensitive_fields` when the use case is clear.
    "mail.message": frozenset(
        {"body", "subject", "author_id", "email_from", "email_to", "email_cc"}
    ),
    # Calendar event descriptions can contain confidential meeting notes
    # (1-on-1s, board topics, acquisition talks). Metadata is fine by default.
    # `videocall_location` and `access_token` give direct join-link access.
    "calendar.event": frozenset({"description", "videocall_location", "access_token"}),
    "calendar.attendee": frozenset({"access_token"}),
}

_BINARY_PLACEHOLDER_PREFIX: Final[str] = "<binary:"

# Whitelisted aggregation functions for read_group `fields` entries.
# Anything outside this set is rejected — no SQL-ish tricks via alias syntax.
_AGG_FUNCS: Final[frozenset[str]] = frozenset(
    {"sum", "avg", "count", "count_distinct", "max", "min"}
)

# Whitelisted date/datetime bucketing suffixes for read_group `groupby` entries.
_GROUPBY_TIME_SUFFIXES: Final[frozenset[str]] = frozenset(
    {"hour", "day", "week", "month", "quarter", "year"}
)

# Hard cap on groupby dimensions. More than this is almost always a mistake
# and risks combinatorial explosion on the Odoo side.
_GROUPBY_MAX_DIMS: Final[int] = 4


def is_always_redacted(field_name: str) -> bool:
    """True if ``field_name`` matches an always-redacted pattern."""
    return any(p.fullmatch(field_name) for p in _ALWAYS_REDACTED_PATTERNS)


def is_always_redacted_with_extra(
    field_name: str,
    extra_patterns: tuple[re.Pattern[str], ...] | None = None,
) -> bool:
    """Like :func:`is_always_redacted`, but also checks per-instance patterns.

    Per-instance patterns come from
    ``InstanceConfig.custom_sensitive_field_patterns`` and are compiled once
    via :func:`compile_extra_patterns`. They are checked with the same
    ``fullmatch`` semantics and have the same effect: matching field names
    are dropped from responses and rejected on writes, regardless of
    ``allow_sensitive_fields``.
    """
    if is_always_redacted(field_name):
        return True
    if extra_patterns is None:
        return False
    return any(p.fullmatch(field_name) for p in extra_patterns)


def compile_extra_patterns(patterns: list[str] | tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    """Compile a list of caller-supplied regex strings.

    Each pattern is compiled with :data:`re.IGNORECASE` so it behaves like
    the built-in always-redacted patterns. Bad regex surface as
    :class:`FieldPolicyError` with the offending pattern in the message.
    """
    compiled: list[re.Pattern[str]] = []
    for raw in patterns:
        try:
            compiled.append(re.compile(raw, re.IGNORECASE))
        except re.error as exc:
            raise FieldPolicyError(f"Invalid custom sensitive-field regex {raw!r}: {exc}") from exc
    return tuple(compiled)


def is_default_hidden(
    model: str,
    field_name: str,
    *,
    instance_overrides: dict[str, frozenset[str]] | None = None,
) -> bool:
    """True if ``field_name`` is default-hidden on ``model``.

    If ``instance_overrides`` is provided AND contains ``model`` as a key, that
    set is used (even if it's empty — an empty set means "no fields hidden for
    this model on this instance"). Otherwise we fall back to
    :data:`_DEFAULT_HIDDEN`.
    """
    if instance_overrides is not None and model in instance_overrides:
        return field_name in instance_overrides[model]
    return field_name in _DEFAULT_HIDDEN.get(model, frozenset())


def validate_requested_fields(
    model: str,
    requested: list[str],
    known_fields: frozenset[str],
    *,
    allow_sensitive: frozenset[str],
    instance_overrides: dict[str, frozenset[str]] | None = None,
    extra_redacted: tuple[re.Pattern[str], ...] = (),
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
            raise FieldPolicyError(f"Field {name!r} does not exist on model {model!r}.")
        if is_always_redacted_with_extra(name, extra_redacted):
            raise FieldPolicyError(f"Field {name!r} is permanently redacted and cannot be read.")
        if (
            is_default_hidden(model, name, instance_overrides=instance_overrides)
            and name not in allow_sensitive
        ):
            raise FieldPolicyError(
                f"Field {name!r} on {model!r} is sensitive and must be explicitly unlocked "
                f"via allow_sensitive_fields=[{name!r}, ...]."
            )
    return list(requested)


def validate_write_values(
    model: str,
    values: dict[str, Any],
    known_fields: frozenset[str],
    *,
    extra_redacted: tuple[re.Pattern[str], ...] = (),
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
            raise FieldPolicyError(f"Field {name!r} does not exist on model {model!r}.")
        if is_always_redacted_with_extra(name, extra_redacted):
            raise FieldPolicyError(
                f"Field {name!r} is protected and cannot be written via the MCP."
            )
        out[name] = value
    return out


def validate_aggregate_fields(
    model: str,
    fields: list[str],
    known_fields: frozenset[str],
    *,
    allow_sensitive: frozenset[str],
    instance_overrides: dict[str, frozenset[str]] | None = None,
    extra_redacted: tuple[re.Pattern[str], ...] = (),
) -> list[str]:
    """Validate the ``fields`` list for ``read_group``.

    Each entry is either ``"<field>"`` (Odoo uses a default aggregation) or
    ``"<field>:<agg>"`` where ``<agg>`` is one of ``sum``, ``avg``, ``count``,
    ``count_distinct``, ``max``, ``min``. Anything else — alias syntax, SQL
    fragments, nested parens — is rejected.

    Same redaction policy as :func:`validate_requested_fields`:
    always-redacted fields are banned unconditionally, default-hidden fields
    require opt-in via ``allow_sensitive``.
    """
    if not isinstance(fields, list) or not fields:
        raise FieldPolicyError(
            "Aggregate field list is required — pass at least one 'field' or 'field:agg'."
        )
    for spec in fields:
        if not isinstance(spec, str) or not spec:
            raise FieldPolicyError(
                f"Aggregate field spec must be a non-empty string, got {spec!r}."
            )
        parts = spec.split(":")
        if len(parts) == 1:
            name = parts[0]
        elif len(parts) == 2:
            name, agg = parts
            if agg not in _AGG_FUNCS:
                raise FieldPolicyError(
                    f"Aggregation {agg!r} not allowed. Use one of {sorted(_AGG_FUNCS)}."
                )
        else:
            raise FieldPolicyError(
                f"Aggregate field spec {spec!r} not supported — use 'field' or 'field:agg'."
            )
        if not name:
            raise FieldPolicyError(f"Aggregate field spec {spec!r} has empty field name.")
        if "." in name:
            raise FieldPolicyError(f"Dotted aggregate field {name!r} not allowed.")
        if name not in known_fields:
            raise FieldPolicyError(f"Aggregate field {name!r} does not exist on model {model!r}.")
        if is_always_redacted_with_extra(name, extra_redacted):
            raise FieldPolicyError(f"Aggregate field {name!r} is permanently redacted.")
        if (
            is_default_hidden(model, name, instance_overrides=instance_overrides)
            and name not in allow_sensitive
        ):
            raise FieldPolicyError(
                f"Aggregate field {name!r} on {model!r} is sensitive and must be "
                f"explicitly unlocked via allow_sensitive_fields=[{name!r}, ...]."
            )
    return list(fields)


def validate_groupby(
    model: str,
    groupby: list[str],
    known_fields: frozenset[str],
    *,
    allow_sensitive: frozenset[str],
    instance_overrides: dict[str, frozenset[str]] | None = None,
    extra_redacted: tuple[re.Pattern[str], ...] = (),
) -> list[str]:
    """Validate the ``groupby`` list for ``read_group``.

    Each entry is either ``"<field>"`` or ``"<date_field>:<granularity>"``
    where ``<granularity>`` is one of ``hour``, ``day``, ``week``, ``month``,
    ``quarter``, ``year``. At most :data:`_GROUPBY_MAX_DIMS` dimensions.

    Default-hidden fields are rejected here even with opt-in is required,
    because grouping echoes the distinct field values in the result — which
    is effectively a read of those values.
    """
    if not isinstance(groupby, list) or not groupby:
        raise FieldPolicyError("groupby is required for read_group — pass at least one dimension.")
    if len(groupby) > _GROUPBY_MAX_DIMS:
        raise FieldPolicyError(
            f"groupby supports at most {_GROUPBY_MAX_DIMS} dimensions, got {len(groupby)}."
        )
    for spec in groupby:
        if not isinstance(spec, str) or not spec:
            raise FieldPolicyError(f"groupby entry must be a non-empty string, got {spec!r}.")
        parts = spec.split(":")
        if len(parts) == 1:
            name = parts[0]
        elif len(parts) == 2:
            name, gran = parts
            if gran not in _GROUPBY_TIME_SUFFIXES:
                raise FieldPolicyError(
                    f"groupby granularity {gran!r} not allowed. "
                    f"Use one of {sorted(_GROUPBY_TIME_SUFFIXES)}."
                )
        else:
            raise FieldPolicyError(
                f"groupby spec {spec!r} not supported — use 'field' or 'date_field:granularity'."
            )
        if not name:
            raise FieldPolicyError(f"groupby spec {spec!r} has empty field name.")
        if "." in name:
            raise FieldPolicyError(f"Dotted groupby field {name!r} not allowed.")
        if name not in known_fields:
            raise FieldPolicyError(f"groupby field {name!r} does not exist on model {model!r}.")
        if is_always_redacted_with_extra(name, extra_redacted):
            raise FieldPolicyError(f"groupby field {name!r} is permanently redacted.")
        if (
            is_default_hidden(model, name, instance_overrides=instance_overrides)
            and name not in allow_sensitive
        ):
            raise FieldPolicyError(
                f"groupby field {name!r} on {model!r} is sensitive — grouping by it "
                f"reveals its distinct values. Opt in via allow_sensitive_fields=[{name!r}, ...]."
            )
    return list(groupby)


def redact_response(
    model: str,
    records: list[dict[str, Any]],
    field_types: dict[str, str],
    *,
    allow_sensitive: frozenset[str],
    include_binary: bool,
    instance_overrides: dict[str, frozenset[str]] | None = None,
    extra_redacted: tuple[re.Pattern[str], ...] = (),
) -> list[dict[str, Any]]:
    """Apply redaction and binary stripping to a batch of records.

    ``field_types`` comes from ``fields_get`` — a mapping of field name to
    its Odoo type string (``"char"``, ``"binary"``, ``"many2one"``, ...).

    We copy each record (rather than mutating) so callers don't accidentally
    retain a reference to the pre-redaction dict.

    **Defense-in-depth on missing field types.** If a returned record
    contains a field name that is NOT present in ``field_types``, we drop
    it. The rationale is conservative: we cannot tell whether the field is
    a binary blob (which would otherwise pass through without the size
    placeholder), and a custom module's field that doesn't appear in
    ``fields_get`` is unusual enough to warrant erring on the side of
    silence rather than potentially blowing up the model context with an
    un-stripped binary. The dispatcher already validates the requested
    field list against ``fields_get`` before calling Odoo, so in practice
    this branch only fires for fields the server appended itself
    (``id``, computed extras) — and those are always type-known.
    """
    out: list[dict[str, Any]] = []
    for rec in records:
        cleaned: dict[str, Any] = {}
        for name, value in rec.items():
            if is_always_redacted_with_extra(name, extra_redacted):
                continue  # drop entirely
            if (
                is_default_hidden(model, name, instance_overrides=instance_overrides)
                and name not in allow_sensitive
            ):
                continue  # drop entirely
            if name not in field_types:
                # No type info — be conservative and drop it (see docstring).
                # `id` is a built-in primary key always present in fields_get,
                # so this branch never fires for normal records.
                continue
            if not include_binary and field_types.get(name) == "binary" and value:
                size = _binary_size_hint(value)
                cleaned[name] = f"{_BINARY_PLACEHOLDER_PREFIX}{size} bytes>"
                continue
            cleaned[name] = value
        out.append(cleaned)
    return out


def redact_fields_get(
    model: str,
    fields_get: dict[str, dict[str, Any]],
    *,
    instance_overrides: dict[str, frozenset[str]] | None = None,
    extra_redacted: tuple[re.Pattern[str], ...] = (),
) -> dict[str, dict[str, Any]]:
    """Filter an ``fields_get`` response by the same policy used at read time.

    Used by ``odoo_describe_model`` so the tool never even advertises the
    existence of always-redacted fields, and marks default-hidden ones so
    Claude knows what opt-in is required.
    """
    out: dict[str, dict[str, Any]] = {}
    for name, meta in fields_get.items():
        if is_always_redacted_with_extra(name, extra_redacted):
            continue
        meta_copy = dict(meta)
        if is_default_hidden(model, name, instance_overrides=instance_overrides):
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
