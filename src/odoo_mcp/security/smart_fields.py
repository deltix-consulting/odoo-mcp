"""Smart field selection for ``odoo_search_read`` / ``odoo_read``.

When a caller omits the ``fields`` argument we pick a curated subset
instead of the full record. Goal: cut tokens by stripping audit fields,
binary blobs, HTML bodies, and relational expansions that the caller
almost never wants by default. The caller can always pass an explicit
``fields=[...]`` list to override.

The selection is deliberately conservative — we'd rather miss a useful
field (caller can add it back) than auto-include an expensive one. The
sensitive-field policy still applies on top of this; smart selection
never bypasses redaction.
"""

from __future__ import annotations

import re
from typing import Any, Final

from .fields import is_always_redacted_with_extra, is_default_hidden

# Field types we drop from smart defaults. Binary and HTML payloads bloat
# every record; one2many / many2many expand to ID lists that are usually
# not what the caller wants without an explicit ask.
_HEAVY_TYPES: Final[frozenset[str]] = frozenset({"binary", "html", "one2many", "many2many"})

# Audit / housekeeping fields Odoo adds to every model. Always skipped.
_AUDIT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "create_uid",
        "create_date",
        "write_uid",
        "write_date",
        "__last_update",
        "message_ids",
        "message_follower_ids",
        "message_partner_ids",
        "message_attachment_count",
        "message_has_error",
        "message_has_error_counter",
        "message_has_sms_error",
        "message_is_follower",
        "message_main_attachment_id",
        "message_needaction",
        "message_needaction_counter",
        "message_unread",
        "message_unread_counter",
        "website_message_ids",
        "activity_ids",
        "activity_state",
        "activity_user_id",
        "activity_type_id",
        "activity_type_icon",
        "activity_date_deadline",
        "activity_summary",
        "activity_exception_decoration",
        "activity_exception_icon",
        "activity_calendar_event_id",
        "rating_ids",
        "rating_last_value",
        "rating_last_feedback",
        "rating_last_image",
        "rating_count",
        "rating_avg",
    }
)

# A small priority list — when present on the model, these fields go
# first in the result so the most useful columns aren't crowded out by
# the cap. Order matters.
_PRIORITY_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "name",
    "display_name",
    "code",
    "ref",
    "reference",
    "state",
    "active",
    "partner_id",
    "user_id",
    "company_id",
    "currency_id",
    "amount_total",
    "date",
    "date_order",
    "invoice_date",
)

# Hard cap on how many fields smart selection returns. 25 is enough for
# any realistic interactive use — if you need more, pass an explicit list.
DEFAULT_SMART_FIELDS_LIMIT: Final[int] = 25

# Fields whose names match this pattern are skipped: low-signal counters
# / flags that follow obvious naming conventions and typically don't add
# value to a default read.
_NOISY_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(has_|is_|_has_|x_studio_image_|kanban_|color$)",
    re.IGNORECASE,
)


def select_smart_fields(
    model: str,
    fields_meta: dict[str, dict[str, Any]],
    *,
    instance_overrides: dict[str, frozenset[str]] | None = None,
    extra_redacted: tuple[re.Pattern[str], ...] = (),
    limit: int = DEFAULT_SMART_FIELDS_LIMIT,
) -> list[str]:
    """Return a curated default field list for a model.

    The returned list is always non-empty (at minimum it contains
    ``id``). Sensitive fields — both always-redacted and default-hidden
    — are excluded; the caller would have to pass them explicitly via
    ``fields=`` plus ``allow_sensitive_fields=`` to see them.
    """
    selected: list[str] = []
    seen: set[str] = set()

    def _consider(fname: str) -> None:
        if fname in seen:
            return
        meta = fields_meta.get(fname)
        if meta is None:
            return
        ftype = str(meta.get("type") or "")
        if ftype in _HEAVY_TYPES:
            return
        if fname in _AUDIT_FIELDS:
            return
        if fname.startswith("__"):
            return
        if _NOISY_NAME_PATTERN.match(fname):
            return
        # Computed-but-not-stored fields: each access triggers a server-side
        # compute and bloats the response with fields the caller didn't ask
        # for. ``store`` is only present in fields_get output when fetched
        # with that attribute (we do); when missing we conservatively keep
        # the field — that matches the pre-v0.14.1 behaviour for L2-cached
        # entries fetched before the attribute was added.
        if "store" in meta and meta.get("store") is False:
            return
        # Sensitive — both always-redacted (passwords / tokens) and
        # default-hidden (vat / iban / employee PII) — are excluded.
        if is_always_redacted_with_extra(fname, extra_redacted):
            return
        if is_default_hidden(model, fname, instance_overrides=instance_overrides):
            return
        selected.append(fname)
        seen.add(fname)

    # Priority pass — in fixed order.
    for fname in _PRIORITY_FIELDS:
        if fname in fields_meta:
            _consider(fname)
            if len(selected) >= limit:
                return selected

    # Fill pass — alphabetical, deterministic.
    for fname in sorted(fields_meta.keys()):
        _consider(fname)
        if len(selected) >= limit:
            break

    # Always at least ``id`` — every consumer needs the record key. If
    # somehow the priority pass skipped it (shouldn't happen on a real
    # Odoo model but the safeguard is cheap), prepend it.
    if "id" not in seen:
        selected.insert(0, "id")
    return selected
