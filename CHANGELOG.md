# Changelog

All notable changes to `odoo-mcp` will be documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While we are still on 0.x the public API (tool names, argument shapes,
config schema) may change between minor versions; we will call out any
breaking change explicitly in this file.

## [Unreleased]

<!-- Add new entries here. -->

## [0.4.1] - 2026-04-22

### Added

- Admin-credential detection. After authentication, the client checks
  whether the API key's user is the Odoo superuser (`uid=1`) or has the
  `base.group_system` group, and exposes the result as `client.is_admin`
  / `client.admin_reason`. Doctor surfaces it as a `!` warning line
  (informational — does not change exit code). `odoo_list_instances`
  includes an `admin_warning` field on the affected instance so Claude
  can see it. The MCP does not refuse to run with admin credentials so
  existing setups keep working, but the warning makes clear that
  per-user Odoo ACL scoping is bypassed and a dedicated non-admin
  user should be created for MCP use.

### Fixed

- Doctor smoke test no longer tries `fields_get("*")` when the instance
  is in open allowlist mode — it now picks `res.partner` as a known
  probe model.

## [0.4.0] - 2026-04-18

### Changed

- **BREAKING**: the default model allowlist is now **open mode**. A fresh
  install (or any config without an explicit `allowed_models`) grants
  access to every Odoo model *except* those on the hardcoded
  `MODEL_DENYLIST`. The wildcard sentinel `"*"` is accepted in TOML
  (`allowed_models = ["*"]`) as the explicit spelling of this mode.
  Users who had an explicit `allowed_models = [...]` list in their
  TOML are unaffected — strict mode continues to work unchanged and
  remains available per-instance for teams that want an enumerated
  allowlist.
- `odoo_list_instances` / `odoo_help` now expose an `allowlist_mode`
  field (`"open"` or `"strict"`) per instance and a top-level
  `denylist_size`. In open mode the response no longer enumerates
  models (it would be misleading to report `["*"]` as if it were a
  concrete set); in strict mode the enumerated list is still returned.

### Added

- `MODEL_DENYLIST` — a hardcoded, non-overrideable set of ~25 models
  that are always blocked, even in open mode. Covers auth / user /
  group tables (`res.users`, `res.groups`, `res.users.apikeys`,
  `auth_totp.device`, ...), ACL / rule definitions (`ir.model.access`,
  `ir.rule`), stored executable content (`ir.actions.server`,
  `ir.actions.client`, `ir.ui.view`, `mail.template`), system
  configuration (`ir.config_parameter`), scheduler / module internals
  (`ir.cron`, `ir.module.module`, `ir.logging`, `ir.sequence`), model
  metadata (`ir.model`, `ir.model.fields`, `ir.model.data`), raw
  attachments (`ir.attachment`), and import/export infrastructure
  (`base_import.import`, `base_import.mapping`). The denylist cannot
  be disabled via config — it is a safety invariant.
- `odoo_archive_or_delete` tool for removing records. Accepts
  `mode="archive"` (sets `active=False`, reversible) or
  `mode="delete"` (calls `unlink`, permanent). The tool description
  instructs Claude to always offer archive first. Full prod-guard /
  dry-run / confirmation-token flow, same as `odoo_create` and
  `odoo_write`. Archive mode refuses to run on a model without an
  `active` field with a clear error pointing at `mode='delete'`.
- New `Operation.ARCHIVE` and `Operation.UNLINK` enum members,
  classified as write ops.
- `odoo-mcp config show` now prints a `denylist:` line and a
  `allowed_models: open mode (...)` summary for instances in open
  mode.

## [0.3.0] - 2026-04-17

### Added

- Expanded default `allowed_models` from 14 to 27 to cover common Odoo
  business modules out of the box: `purchase.order`, `stock.picking`,
  `stock.move`, `planning.slot`, `hr.expense`, `hr.expense.sheet`,
  `helpdesk.ticket`, `knowledge.article`, `approval.request`,
  `calendar.event`, `documents.document`, `mail.message`, plus
  `account.analytic.line`. Existing per-instance `allowed_models`
  overrides in user TOML continue to take precedence.

### Security

- `mail.message` ships with a strict default-hidden policy on `body`,
  `subject`, `author_id`, `email_from`, `email_to`, `email_cc` because
  it is a cross-model side-door that can reference any `res_model`.
  Callers must opt in per-field via `allow_sensitive_fields`.
- `calendar.event.description` is default-hidden — can contain
  confidential meeting notes. Metadata (title, attendees, times) is
  still returned by default.

## [0.2.0] - 2026-04-17

First tracked release. This entry captures the full set of features
present in `0.1.0`, since prior changes were not logged.

### Added

- MCP tools exposed over stdio:
  - `odoo_list_instances` — list configured instances and their state.
  - `odoo_describe_model` — `fields_get` for one allowlisted model,
    with redaction markers.
  - `odoo_search_read` — search + read with explicit `fields` list and
    sandboxed domain.
  - `odoo_search_count` — count records matching a domain.
  - `odoo_read_group` — aggregate with `sum`/`avg`/`count`/
    `count_distinct`/`max`/`min`, groupby with date-granularity
    suffixes, capped at four dimensions.
  - `odoo_read` — read specific records by ID.
  - `odoo_create` — create a record (prod-gated).
  - `odoo_write` — update records (prod-gated).
  - `odoo_enable_prod_writes` — 15-minute activity-based write unlock
    for production instances.
- Setup wizard (`odoo-mcp setup`) with subcommands: `--add`, `--remove`,
  `--list`, `--rotate-key NAME`, `--regenerate-launcher`. The wizard
  generates `config.toml` (chmod 600), `launch.sh` (chmod 700),
  stores credentials in the macOS Keychain, and registers the server
  in Claude Desktop's config.
- `odoo-mcp doctor` — pre-flight health check covering config
  permissions, TOML parse, audit log writability, credential loading,
  TLS, authentication, and a smoke `fields_get` call.
- `odoo-mcp status` — live status of configured instances:
  authentication state, unlock state, rate-limit budget.
- `odoo-mcp audit` — interactive inspector for the JSONL audit log.
- Lazy authentication: instances authenticate against Odoo on first
  use, not at server startup, so one unreachable instance does not
  block all others.
- macOS Keychain integration for credentials. Values are pulled at
  launch by `launch.sh`, exported into the server process, and deleted
  from `os.environ` after the credential objects are constructed.
- JSONL audit log at `~/.odoo-mcp/audit.jsonl` with daily rotation and
  30-day retention. Logs metadata only (timestamp, instance, tool,
  model, operation, counts, duration, dry-run flag, result code).
  Never logs field values, domain operands, or credentials.
- Per-instance token-bucket rate limiter.
- Per-call XML-RPC timeout, configurable via `timeout_seconds` in
  `[defaults]`.
- Frozen Odoo context: the client constructs its own context and
  never forwards caller-supplied context keys.
- CI workflow: ruff, ruff format, mypy strict, pytest.
- 140 unit tests covering every security layer.

### Security

- **Model allowlist**: every tool call is checked against a
  per-instance frozen set. Default covers the common CRM / sales /
  accounting / project / HR surface; `res.users`, `ir.*`, and
  `ir.config_parameter` are intentionally excluded.
- **Operation allowlist**: closed enum of seven operations
  (`search_read`, `search_count`, `read`, `read_group`, `create`,
  `write`, `fields_get`). No `unlink`. No `execute_kw`.
- **Domain sandbox**: dotted field paths in domains (e.g.
  `create_uid.login`) are rejected, so callers cannot traverse from
  `crm.lead` into `res.users`. Unknown operators are rejected.
- **Field redaction — always**: fields matching
  `password`, `*_password`, `password_crypt`, `new_password`,
  `api_key`, `*_api_key`, `token`, `*_token`, `access_token`,
  `refresh_token`, `*_secret` are dropped from every response and
  blocked in every write payload.
- **Field redaction — default-hidden**: per-model PII (VAT, bank
  accounts, company registry, employee SSN / private contact / family
  details, payment partner bank) requires per-call
  `allow_sensitive_fields=[...]` opt-in. Grouping by a default-hidden
  field is also gated, since groupby reveals distinct values.
- **Binary stripping**: binary fields are replaced with a
  `<binary:N bytes>` placeholder unless the caller passes
  `include_binary=true`.
- **Production guard**: writes on `production = true` instances are
  blocked by default. `odoo_enable_prod_writes` grants a 15-minute
  activity-refreshed unlock. Writes default to `dry_run=true` on
  prod; real commits require `dry_run=false` plus a single-use
  `confirmation_token` issued by a prior dry run and bound to
  `(instance, operation, model)`.
- **Credential scrubbing**: `OdooMcpError` and subclasses scrub
  registered secret values from their string form, including chained
  causes. `Credentials.__repr__` returns a redaction placeholder.
- **Audit fail-closed**: if the audit log cannot be written, the
  server refuses every tool call until the log is writable again.
- **Config permission check**: `config.toml` must be `chmod 600`;
  the server refuses to start otherwise.
- **Strict TLS**: production instances must use HTTPS; self-signed
  certificates are rejected on prod regardless of
  `allow_self_signed`.
- **Frozen context**: the XML-RPC client never forwards
  caller-supplied Odoo context, so callers cannot use `context` to
  toggle server-side behaviour (`no_validate`, `mail_create_nolog`,
  etc.).
- **Record count caps**: `limit` is clamped to
  `max_records_hard_cap`; `ids` lists for `read` and `write` are
  also capped.
