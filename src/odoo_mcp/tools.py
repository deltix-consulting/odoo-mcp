"""MCP tool schemas.

Each tool is defined as a module-level constant so it can be found, diffed,
and unit-tested in isolation. The ergonomics for Claude are much better
when descriptions explain the security model rather than just the mechanical
argument types, so we hand-write each schema.
"""

from __future__ import annotations

from mcp.types import Tool

_TOOL_LIST_INSTANCES = Tool(
    name="odoo_list_instances",
    description=(
        "List the configured Odoo instances the MCP can talk to. Returns for each "
        "instance: name, production flag, whether prod writes are currently unlocked, "
        "and the allowlist mode ('open' = any non-denylisted model is reachable, "
        "'strict' = explicit enumerated list). Safe, read-only. "
        "Example: use this first if you don't know the instance names."
    ),
    inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
)

_TOOL_DESCRIBE_MODEL = Tool(
    name="odoo_describe_model",
    description=(
        "Return the field metadata (fields_get) for one allowlisted Odoo model. "
        "Default response is minimal: per field {type, string, required?, _sensitive?} "
        "— enough to pick fields without paragraphs of help text per field. "
        "Pass `verbose=true` for the full schema (help, relation, readonly, _note). "
        "Permanently-redacted fields (passwords, tokens) are omitted entirely; "
        "default-hidden sensitive fields (VAT, IBAN, employee PII) are marked "
        "with `_sensitive: true` so you know they require explicit unlock. "
        'Example: model="res.partner" returns fields like id, name, email, vat (marked sensitive).'
    ),
    inputSchema={
        "type": "object",
        "required": ["instance", "model"],
        "additionalProperties": False,
        "properties": {
            "instance": {"type": "string", "description": "Configured instance name."},
            "model": {
                "type": "string",
                "description": "Odoo model name (e.g. 'res.partner').",
            },
            "verbose": {
                "type": "boolean",
                "default": False,
                "description": (
                    "If true, include help text, relation, readonly, and _note for "
                    "each field (much larger response — useful when designing "
                    "complex writes or studying a custom model)."
                ),
            },
        },
    },
)

_TOOL_LOOKUP = Tool(
    name="odoo_lookup",
    description=(
        "Fast name-based lookup. Searches the model's `name` field for "
        "case-insensitive substring matches and returns only id + "
        "display_name — much cheaper than odoo_search_read for finding "
        "records by name. Domain sandbox does not apply (the search is "
        "fixed to a `name ilike` filter). Sensitive-field policy still "
        "applies: if `display_name` resolves to a sensitive field (e.g. "
        "for some custom HR models), the result is redacted. "
        "Example: model='res.partner', query='Acme' returns up to limit "
        "matching companies / contacts."
    ),
    inputSchema={
        "type": "object",
        "required": ["instance", "model", "query"],
        "additionalProperties": False,
        "properties": {
            "instance": {"type": "string"},
            "model": {"type": "string"},
            "query": {
                "type": "string",
                "minLength": 1,
                "description": "Substring to match (ilike).",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "default": 10,
                "description": "Max results.",
            },
        },
    },
)


_TOOL_SEARCH_READ = Tool(
    name="odoo_search_read",
    description=(
        "Run Odoo search_read against an allowlisted model. Pass an explicit "
        "`fields` list, OR omit it to get a curated default (id + name + a few "
        "business-relevant scalars, audit/binary/HTML/relation fields skipped). "
        "Domain filters are sandboxed: dotted field traversal (e.g. "
        "'create_uid.login') is rejected. Results have default-hidden fields "
        "stripped unless you pass `allow_sensitive_fields`. Binary fields are "
        "replaced with a size placeholder unless you pass `include_binary=true`. "
        'Example: find active companies: domain=[["is_company","=",true],["active","=",true]], '
        'fields=["id","name","email"], limit=20.'
    ),
    inputSchema={
        "type": "object",
        "required": ["instance", "model"],
        "additionalProperties": False,
        "properties": {
            "instance": {"type": "string"},
            "model": {"type": "string"},
            "domain": {
                "type": "array",
                "description": (
                    "Odoo domain: list of (field, op, value) tuples plus optional "
                    "'&'/'|'/'!' prefix operators. Dotted fields are rejected."
                ),
                "default": [],
            },
            "fields": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": (
                    "Explicit list of fields to return. Optional — omit to "
                    "use the smart default (a curated subset of safe scalar "
                    "fields, capped at 25)."
                ),
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "description": "Max records to return. Defaults to the instance default, clamped to the hard cap.",
            },
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "order": {
                "type": "string",
                "description": "Odoo order string, e.g. 'name asc'.",
            },
            "allow_sensitive_fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Per-call opt-in to default-hidden sensitive fields.",
                "default": [],
            },
            "include_binary": {"type": "boolean", "default": False},
        },
    },
)

_TOOL_SEARCH_COUNT = Tool(
    name="odoo_search_count",
    description=(
        "Count records matching a domain on an allowlisted model. Returns a "
        "single integer — much cheaper than fetching records just to count them. "
        "Same domain sandbox as odoo_search_read: dotted fields and unknown "
        "operators are rejected. Read-only. "
        'Example: count open opportunities: domain=[["type","=","opportunity"]]. '
        'Returns {"count": N}.'
    ),
    inputSchema={
        "type": "object",
        "required": ["instance", "model"],
        "additionalProperties": False,
        "properties": {
            "instance": {"type": "string"},
            "model": {"type": "string"},
            "domain": {
                "type": "array",
                "description": "Odoo domain; same rules as odoo_search_read.",
                "default": [],
            },
        },
    },
)

_TOOL_READ_GROUP = Tool(
    name="odoo_read_group",
    description=(
        "Aggregate records via Odoo's read_group. Use this for dashboards and "
        "summaries instead of fetching records to count / sum them yourself. "
        "Aggregate `fields` syntax: 'field' (default agg) or 'field:AGG' where "
        "AGG is sum|avg|count|count_distinct|max|min. `groupby` syntax: 'field' "
        "or 'date_field:GRAN' where GRAN is day|week|month|quarter|year|hour. "
        "At most 4 groupby dimensions. Sensitive fields require "
        "allow_sensitive_fields opt-in; always-redacted fields are blocked. "
        'Example: leads per stage: fields=["id:count"], groupby=["stage_id"]. '
        'Revenue per month: fields=["expected_revenue:sum"], groupby=["create_date:month"].'
    ),
    inputSchema={
        "type": "object",
        "required": ["instance", "model", "fields", "groupby"],
        "additionalProperties": False,
        "properties": {
            "instance": {"type": "string"},
            "model": {"type": "string"},
            "domain": {"type": "array", "default": []},
            "fields": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": "Aggregate specs, e.g. ['amount_total:sum', 'id:count'].",
            },
            "groupby": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 4,
                "description": "Dimensions, e.g. ['stage_id'] or ['date_order:month', 'user_id'].",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "description": "Max groups to return. Clamped to the instance hard cap.",
            },
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "orderby": {
                "type": "string",
                "description": "Order string, e.g. 'amount_total desc'.",
            },
            "lazy": {
                "type": "boolean",
                "default": True,
                "description": (
                    "Odoo lazy flag: if true, only the first groupby dimension is "
                    "aggregated and child groups are returned as drill-down specs. "
                    "Set to false to compute all dimensions eagerly."
                ),
            },
            "allow_sensitive_fields": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
            },
            "include_domain": {
                "type": "boolean",
                "default": False,
                "description": (
                    "If true, include the per-group `__domain` (a literal "
                    "domain-list for drill-down) in each row. Off by default "
                    "to keep the response small."
                ),
            },
        },
    },
)

_TOOL_READ = Tool(
    name="odoo_read",
    description=(
        "Fetch specific records by ID from an allowlisted model. Same field-level "
        "policies as odoo_search_read: pass `fields` explicitly OR omit for the "
        "smart default; sensitive fields gated; binary fields stripped by default. "
        'Example: ids=[42, 47], fields=["name","email"] fetches two partners.'
    ),
    inputSchema={
        "type": "object",
        "required": ["instance", "model", "ids"],
        "additionalProperties": False,
        "properties": {
            "instance": {"type": "string"},
            "model": {"type": "string"},
            "ids": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 1,
                "description": "Record IDs to read.",
            },
            "fields": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": (
                    "Explicit list of fields to return. Optional — omit "
                    "for the smart default (capped at 25 fields)."
                ),
            },
            "allow_sensitive_fields": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
            },
            "include_binary": {"type": "boolean", "default": False},
        },
    },
)

_TOOL_CREATE = Tool(
    name="odoo_create",
    description=(
        "Create a record in an allowlisted model. Writes to production instances "
        "are blocked unless odoo_enable_prod_writes has been called first, AND the "
        "call defaults to dry_run=true on prod so you MUST pass dry_run=false AND a "
        "confirmation_token from a prior dry-run call to actually commit. Protected "
        "fields (passwords, tokens) cannot be written. "
        'Example: create a lead: model="crm.lead", values={"name":"Acme opportunity",'
        '"contact_name":"Jane"}. On prod: dry_run first to get a token.'
    ),
    inputSchema={
        "type": "object",
        "required": ["instance", "model", "values"],
        "additionalProperties": False,
        "properties": {
            "instance": {"type": "string"},
            "model": {"type": "string"},
            "values": {
                "type": "object",
                "description": "Field -> value mapping. Field names must be known on the model.",
            },
            "dry_run": {
                "type": "boolean",
                "description": "Preview only — validates and returns what would happen. Default: true on prod, false on dev.",
            },
            "confirmation_token": {
                "type": "string",
                "description": "Token from a prior dry-run call, required to commit on prod.",
            },
        },
    },
)

_TOOL_WRITE = Tool(
    name="odoo_write",
    description=(
        "Update existing records on an allowlisted model. Same prod guardrails as "
        "odoo_create: blocked on prod without unlock, dry_run default on prod, "
        "confirmation_token required for real commit. "
        'Example: update a lead stage: ids=[42], values={"stage_id": 3}. '
        "On prod: dry_run first to get a token."
    ),
    inputSchema={
        "type": "object",
        "required": ["instance", "model", "ids", "values"],
        "additionalProperties": False,
        "properties": {
            "instance": {"type": "string"},
            "model": {"type": "string"},
            "ids": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 1,
            },
            "values": {"type": "object"},
            "dry_run": {"type": "boolean"},
            "confirmation_token": {"type": "string"},
        },
    },
)

_TOOL_ARCHIVE_OR_DELETE = Tool(
    name="odoo_archive_or_delete",
    description=(
        "Archive OR permanently delete records. ALWAYS ask the user which "
        "they want before calling this tool — archiving is almost always "
        "what they mean when they say 'delete'. "
        "mode='archive' sets active=False: reversible, preserves history, "
        'can be undone via odoo_write values={"active": true}. '
        "mode='delete' calls unlink: PERMANENT, cannot be undone, erases "
        "all data and linked references. Use delete only when the user "
        "says 'permanently', 'purge', 'remove forever', or explicitly "
        "rejects archiving. "
        "Same prod-guard as odoo_create / odoo_write: dry_run=true (default "
        "on prod) returns a preview + confirmation_token; commit requires "
        "dry_run=false AND the token. "
        "Example: user asks 'delete these 3 old leads' -> ask 'archive or "
        "permanently delete? (archiving is reversible)' -> if they say "
        "archive: mode='archive', ids=[...]."
    ),
    inputSchema={
        "type": "object",
        "required": ["instance", "model", "ids", "mode"],
        "additionalProperties": False,
        "properties": {
            "instance": {"type": "string"},
            "model": {"type": "string"},
            "ids": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 1,
            },
            "mode": {
                "type": "string",
                "enum": ["archive", "delete"],
                "description": (
                    "archive=reversible (sets active=False). delete=permanent (unlink)."
                ),
            },
            "dry_run": {"type": "boolean"},
            "confirmation_token": {"type": "string"},
        },
    },
)


_TOOL_ENABLE_PROD_WRITES = Tool(
    name="odoo_enable_prod_writes",
    description=(
        "Unlock writes to a production instance for the next 15 minutes. Every "
        "subsequent write still defaults to dry_run=true on prod and requires a "
        "confirmation_token to commit. This tool is the explicit step that moves "
        "the session from 'prod is read-only' to 'prod writes allowed'. "
        'Example: instance="prod". Unlocks writes for 15 minutes; each write still '
        "needs dry-run + token."
    ),
    inputSchema={
        "type": "object",
        "required": ["instance"],
        "additionalProperties": False,
        "properties": {"instance": {"type": "string"}},
    },
)


_TOOL_SEND_MESSAGE = Tool(
    name="odoo_send_message",
    description=(
        "Post a message on a record — sends an email when message_type='comment' "
        "AND partner_ids contains at least one recipient (or the record has "
        "followers); creates an internal log note when message_type='notification'. "
        "DISABLED BY DEFAULT. Two independent opt-ins required: "
        "(1) the operator sets ODOO_MCP_ENABLE_EXTERNAL_COMMS=1 in the "
        "environment, (2) the operator sets external_comms_enabled=true on the "
        "target instance in config.toml. Both gates must be set, otherwise this "
        "tool refuses. Then the call goes through the same prod-guard flow as "
        "writes: unlock + dry-run + confirmation_token. The dry-run preview "
        "shows the full body and recipient list verbatim. ALWAYS dry-runs first, "
        "on prod AND dev — outbound emails are equally costly to send by "
        'accident anywhere. Example: model="res.partner", record_id=42, '
        'message_type="comment", body="Hello", partner_ids=[42] sends an email '
        "to partner 42 on a dry-run-then-confirm flow."
    ),
    inputSchema={
        "type": "object",
        "required": ["instance", "model", "record_id", "body"],
        "additionalProperties": False,
        "properties": {
            "instance": {"type": "string"},
            "model": {
                "type": "string",
                "description": "Target model (must pass the allowlist, e.g. 'res.partner').",
            },
            "record_id": {
                "type": "integer",
                "description": "Record id on which to post the message.",
            },
            "body": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Message body. HTML is accepted; plain text gets wrapped "
                    "in <p> by Odoo. Shown verbatim in the dry-run preview."
                ),
            },
            "subject": {
                "type": "string",
                "description": "Email subject (optional). Only relevant when message_type='comment'.",
            },
            "message_type": {
                "type": "string",
                "enum": ["comment", "notification"],
                "default": "comment",
                "description": (
                    "'comment' = visible chatter message + email to partner_ids "
                    "and followers. 'notification' = internal log note, no email."
                ),
            },
            "partner_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "default": [],
                "description": "Recipient res.partner ids. For 'comment' type, these get the email.",
            },
            "dry_run": {
                "type": "boolean",
                "description": (
                    "Preview only — validates and returns what would be sent. "
                    "Defaults to true on EVERY instance (prod and dev), not just prod. "
                    "Pass dry_run=false plus a confirmation_token to commit."
                ),
            },
            "confirmation_token": {
                "type": "string",
                "description": "Token from a prior dry-run call, required to commit.",
            },
        },
    },
)


_TOOL_RUN_DOCUMENT_ACTION = Tool(
    name="odoo_run_document_action",
    description=(
        "Run a document workflow action — confirm / cancel / post / validate — "
        "on one or more records. This is NOT a generic method runner: the "
        "(model, action) pair must exist in a hardcoded map. Supported pairs: "
        "purchase.order confirm|cancel, sale.order confirm|cancel, "
        "account.move post|cancel, stock.picking validate|cancel. Any other "
        "pair is refused. Same prod guardrails as odoo_write: on production "
        "dry_run defaults to true and returns a preview (with each record's "
        "current state) plus a confirmation_token; a real commit needs "
        "dry_run=false AND the token. Use this instead of odoo_write to "
        "change a document's state — writing the state field directly skips "
        "Odoo's workflow logic (no stock moves, no pickings) and corrupts "
        'the record. Example: confirm a purchase order — model="purchase.order", '
        'record_ids=[977], action="confirm".'
    ),
    inputSchema={
        "type": "object",
        "required": ["instance", "model", "record_ids", "action"],
        "additionalProperties": False,
        "properties": {
            "instance": {"type": "string"},
            "model": {
                "type": "string",
                "description": ("purchase.order, sale.order, account.move, or stock.picking."),
            },
            "record_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 1,
                "description": "Record IDs to run the action on.",
            },
            "action": {
                "type": "string",
                "enum": ["confirm", "cancel", "post", "validate"],
                "description": (
                    "Semantic action. Resolved to the Odoo method via the "
                    "hardcoded map; not all actions apply to all models."
                ),
            },
            "dry_run": {
                "type": "boolean",
                "description": (
                    "Preview only — returns each record's current state plus a "
                    "confirmation_token. Default: true on prod, false on dev."
                ),
            },
            "confirmation_token": {
                "type": "string",
                "description": "Token from a prior dry-run call, required to commit on prod.",
            },
        },
    },
)


_TOOL_DIAGNOSE_ACCESS = Tool(
    name="odoo_diagnose_access",
    description=(
        "Diagnose what the authenticated Odoo user can do on a model. Returns "
        "read/write/create/unlink booleans (Odoo's check_access_rights), the "
        "user's uid and login, and admin-status. Useful when a search returns "
        "fewer records than expected, or when planning a write to a model the "
        "user may not have rights to. Read-only — does not modify anything. "
        'Example: instance="prod", model="account.move" returns '
        '{"can_read": true, "can_write": false, ...}.'
    ),
    inputSchema={
        "type": "object",
        "required": ["instance", "model"],
        "additionalProperties": False,
        "properties": {
            "instance": {"type": "string"},
            "model": {
                "type": "string",
                "description": "Odoo model to check (must pass the allowlist).",
            },
        },
    },
)


_TOOL_LOG_NOTE = Tool(
    name="odoo_log_note",
    description=(
        "Post an internal log note on an Odoo record (visible in the "
        "record's chatter; NEVER emailed). Bounded sibling of "
        "``odoo_send_message`` for the audit-trail / internal-note use "
        "case: hardcoded ``message_type='notification'`` + "
        "``subtype_xmlid='mail.mt_note'``, empty partner_ids — so it "
        "physically cannot trigger an email. Because of that guarantee "
        "it does NOT require the ``ODOO_MCP_ENABLE_EXTERNAL_COMMS`` env "
        "var or ``external_comms_enabled = true`` per instance — those "
        "exist to guard the email path, which this tool does not have. "
        "``model`` still goes through the per-instance allowlist plus "
        "the write-blocklist (you cannot log on ``mail.message`` "
        "itself). Same prod-guard pipeline as every other write: "
        "dry-run preview + confirmation_token + commit. The token "
        "binds to ``(record_id, body)``. "
        'Example: model="sale.order", record_id=42, '
        'body="Spoke to customer; PO replaced by 4711".'
    ),
    inputSchema={
        "type": "object",
        "required": ["instance", "model", "record_id", "body"],
        "additionalProperties": False,
        "properties": {
            "instance": {"type": "string"},
            "model": {
                "type": "string",
                "description": "Target model (must pass the allowlist).",
            },
            "record_id": {
                "type": "integer",
                "description": "Record id on which to log the note.",
            },
            "body": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Note body. HTML is accepted; plain text gets wrapped in <p>. "
                    "Shown verbatim in the dry-run preview."
                ),
            },
            "dry_run": {
                "type": "boolean",
                "description": (
                    "Preview only. Defaults to true on production instances and "
                    "false on non-production. Pass false + a confirmation_token "
                    "to commit on production."
                ),
            },
            "confirmation_token": {
                "type": "string",
                "description": "Token from a prior dry-run, required to commit on production.",
            },
        },
    },
)


_TOOL_CREATE_ATTACHMENT = Tool(
    name="odoo_create_attachment",
    description=(
        "Attach a file to an Odoo record. Bounded surface for creating "
        "``ir.attachment`` rows — that model stays read-blocked via the "
        "global denylist (no search_read of arbitrary attachments) and "
        "this tool can only CREATE, never read or unlink. ``res_model`` "
        "still goes through the per-instance allowlist plus the write-"
        "blocklist, so attaching to e.g. ``mail.message`` is refused. "
        "Two input modes, exactly one required: "
        "(1) ``datas_base64`` for SMALL inline payloads the agent typed "
        "itself — note that several agent SDKs silently drop turns when "
        "the inline base64 exceeds ~5 KB, so reach for source_path for "
        "anything larger than a tiny snippet; "
        "(2) ``source_path`` (absolute path) for files the api-server "
        "wrote to a directory listed in the instance's "
        "``attachment_source_paths`` TOML allowlist — the MCP reads + "
        "encodes server-side, so the bytes never traverse the agent "
        "context. Goes through the standard prod-guard pipeline: dry-run "
        "preview (filename + decoded size + target record) → "
        "confirmation_token → commit. The token binds to the exact file "
        "bytes regardless of input mode, so an agent cannot dry-run a "
        "placeholder and commit a different file with the same token. "
        "Decoded size cap: 25 MB. Filename must contain no path "
        "separators. The target record must exist for the authenticated "
        "user. "
        'Example: res_model="account.move", res_id=123, '
        'filename="invoice.pdf", source_path="/var/run/inbox/inv-2026-001.pdf"'
        " attaches the file on disk to invoice 123."
    ),
    inputSchema={
        "type": "object",
        "required": ["instance", "res_model", "res_id", "filename"],
        "additionalProperties": False,
        "properties": {
            "instance": {"type": "string"},
            "res_model": {
                "type": "string",
                "description": (
                    "Target model the file is attached to (must pass the "
                    "allowlist; write-blocklisted models are refused)."
                ),
            },
            "res_id": {
                "type": "integer",
                "description": "Existing record id on the target model.",
            },
            "filename": {
                "type": "string",
                "minLength": 1,
                "maxLength": 255,
                "description": (
                    "Display name of the attachment. No path separators "
                    "('/' or '\\\\') — pass the leaf name only."
                ),
            },
            "datas_base64": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Standard-base64 encoded file content. A ``data:...;base64,`` "
                    "prefix is tolerated and stripped. Decoded size cap: 25 MB. "
                    "Mutually exclusive with ``source_path``; for payloads larger "
                    "than a few KB use ``source_path`` instead — several agent "
                    "SDKs silently drop turns when the inline base64 grows past "
                    "~5 KB."
                ),
            },
            "source_path": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Absolute filesystem path on the MCP host. The MCP reads "
                    "+ base64-encodes the file server-side so the bytes never "
                    "traverse the agent context. The path must resolve (via "
                    "``realpath``) into one of the directories listed in this "
                    "instance's ``attachment_source_paths`` TOML allowlist; "
                    "default is empty, so source_path is refused unless the "
                    "operator opts in. Mutually exclusive with ``datas_base64``."
                ),
            },
            "mimetype": {
                "type": "string",
                "description": (
                    "Optional MIME type (e.g. 'application/pdf'). When omitted, "
                    "Odoo's own detection runs from the filename extension."
                ),
            },
            "description": {
                "type": "string",
                "description": "Optional human-readable description stored on the attachment row.",
            },
            "dry_run": {
                "type": "boolean",
                "description": (
                    "Preview only. Defaults to true on production instances "
                    "and false on non-production. Pass false + a "
                    "confirmation_token from the preview to commit."
                ),
            },
            "confirmation_token": {
                "type": "string",
                "description": "Token from a prior dry-run, required to commit on production.",
            },
        },
    },
)


_TOOL_DIAGNOSE_ROUTING = Tool(
    name="odoo_diagnose_routing",
    description=(
        "Diagnose Odoo's stock-routing config for a (product, warehouse) "
        "pair. Returns: the product's active routes (own + template + "
        "warehouse-derived), the warehouse's delivery/reception config, "
        "and every candidate stock.rule that could fire — each with its "
        "source/destination locations and picking_type_id. Use this when "
        "a sale_order/purchase_order produces an unexpected transfer "
        "type (e.g. 'TR-LAAD' instead of 'LAAD') and you need to know "
        "which stock.rule Odoo would evaluate. Read-only. Does NOT "
        "predict the winning rule — that depends on sequence + "
        "location-chain + custom overrides; the tool lists the "
        "candidates and the operator/agent picks the match. "
        'Example: instance="prod", product_id=8, warehouse_id=2.'
    ),
    inputSchema={
        "type": "object",
        "required": ["instance", "product_id", "warehouse_id"],
        "additionalProperties": False,
        "properties": {
            "instance": {"type": "string"},
            "product_id": {
                "type": "integer",
                "description": "ID of the product.product record being shipped.",
            },
            "warehouse_id": {
                "type": "integer",
                "description": "ID of the stock.warehouse the order targets.",
            },
        },
    },
)


_TOOL_HELP = Tool(
    name="odoo_help",
    description=(
        "Returns a capability overview of this Odoo MCP. Default response is "
        "terse: a one-sentence summary, a tool list with one-liners, and the "
        "configured instances. Pass `verbose=true` for the full cookbook "
        "(common patterns with examples + gotchas) — useful at session start. "
        "Never contacts Odoo."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "verbose": {
                "type": "boolean",
                "default": False,
                "description": "If true, include common_patterns (with examples) and gotchas.",
            },
        },
        "additionalProperties": False,
    },
)


def build_tools() -> list[Tool]:
    """Return the static list of tool schemas in the order Claude sees them."""
    return [
        _TOOL_HELP,
        _TOOL_LIST_INSTANCES,
        _TOOL_DESCRIBE_MODEL,
        _TOOL_LOOKUP,
        _TOOL_SEARCH_READ,
        _TOOL_SEARCH_COUNT,
        _TOOL_READ_GROUP,
        _TOOL_READ,
        _TOOL_CREATE,
        _TOOL_WRITE,
        _TOOL_ARCHIVE_OR_DELETE,
        _TOOL_ENABLE_PROD_WRITES,
        _TOOL_DIAGNOSE_ACCESS,
        _TOOL_DIAGNOSE_ROUTING,
        _TOOL_SEND_MESSAGE,
        _TOOL_LOG_NOTE,
        _TOOL_RUN_DOCUMENT_ACTION,
        _TOOL_CREATE_ATTACHMENT,
    ]
