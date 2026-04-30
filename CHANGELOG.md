# Changelog

All notable changes to `odoo-mcp` will be documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While we are still on 0.x the public API (tool names, argument shapes,
config schema) may change between minor versions; we will call out any
breaking change explicitly in this file.

## [Unreleased]

<!-- Add new entries here. -->

## [0.7.1] - 2026-04-30

### Fixed

- **Confirmation tokens are now bound to the unlock window that issued
  them.** Previously the 5-minute token TTL was independent of the
  15-minute unlock TTL, so a token created during one unlock could be
  redeemed against a *different* later unlock as long as the token
  itself had not yet expired. That broke the review-then-commit
  property: a dry run reviewed under window A could end up committing
  under window B. Tokens now record the issuing unlock's identity
  (`_UnlockState.unlocked_at`); `consume_pending` rejects with
  "token issued under a different unlock window" when the current
  unlock state does not match. `touch()` extends the same window and
  preserves the identity.
- **Audit-log breakage on the failure path is no longer silent.**
  `Dispatcher._audit_failure` previously swallowed `AuditLogError`
  via `contextlib.suppress`, which meant a broken audit log silently
  dropped failure events — the security-interesting ones. The handler
  now logs `audit log write failed during failure path: ...` at
  `ERROR` level via the standard `logging` module so operators with
  `ODOO_MCP_LOG_LEVEL=ERROR` see the breakage. The original tool-call
  error is still returned to the caller (no double-fault). The
  success path remains fail-loud — that asymmetry is by design and
  is now documented in the docstring.
- **`scripts/install.sh` now verifies release attestations before
  extracting the tarball.** The README and `SECURITY.md` already
  advertised attestation verification, but it only ran in
  `odoo-mcp update`. First install — when a colleague is most
  exposed — used to extract unverified. The installer now runs
  `gh attestation verify --owner deltix-consulting
  --signer-workflow ".github/workflows/release.yml"` between download
  and extract. Hard verification failures (signature mismatch) abort
  with a red error; environmental failures (gh not authed, "no
  attestations" on free-tier orgs) print a yellow warning and prompt
  to proceed. New `--skip-verification` flag bypasses the check; the
  prompt is also auto-skipped on non-interactive shells.
- **`MODEL_DENYLIST` contents are now pinned by an explicit
  regression test.** The denylist is the single most security-
  critical constant in the codebase, and the existing test suite
  only checked behavior given a denylist — a refactor that
  accidentally trimmed `res.users` would have passed CI. The new
  test enumerates every required entry; adding a model now requires
  updating both the denylist and the test, and removing one trips
  CI before merge.

## [0.7.0] - 2026-04-30

### Added

- **`odoo-mcp uninstall`** — single-command offboarding. Removes Keychain
  entries for every configured instance, deletes the `odoo-mcp` entry
  from Claude Desktop config (other MCPs preserved), drops
  `~/.odoo-mcp/` (config, launcher, audit logs, fields cache), and runs
  `uv tool uninstall odoo-mcp` best-effort. The project checkout is
  intentionally left alone — print-and-tell so a stale work tree never
  gets nuked. Same flag is also reachable via
  `odoo-mcp setup --uninstall`.
- **Pre-flight Internal-User check** in the setup wizard. After `doctor`
  passes, the wizard authenticates the new instance once more and runs
  `res.users has_group base.group_user`. If the API key belongs to a
  portal / external / shared user the wizard prints a clear warning so
  the user (or their Odoo admin) can fix permissions before the first
  tool call appears mysteriously broken. Best-effort: any error is
  swallowed so the check never fails the wizard.

### Changed

- **Faster MCP startup.** New `python -m odoo_mcp launch` subcommand
  loads Keychain credentials into `os.environ` and starts the server in
  one Python process. The new launcher template skips the previous
  `eval "$(launch-env)"` round-trip, removing one full Python
  interpreter startup (~150-300 ms) on every Claude Cowork launch.
  Existing launchers auto-migrate on the next `odoo-mcp update`. The
  legacy `launch-env` subcommand stays for backward compat.

### Fixed

- **Atomic config writes.** Both `~/.odoo-mcp/config.toml` and Claude
  Desktop's `claude_desktop_config.json` now go through a temp-file +
  `os.replace` write that survives interruption. Before, a Ctrl+C / OOM
  / disk-full mid-write could leave either file truncated or
  half-written, which on Claude Desktop manifested as "MCP not loading
  on next start". Temp files are cleaned up on every exception path.
- **Bounded in-memory fields cache.** The per-`OdooClient` `_fields_cache`
  is now an LRU capped at 64 models (configurable via
  `fields_cache_max_size`). Long-running MCP processes that touched many
  distinct models could grow the cache without bound. The persistent L2
  SQLite cache is unchanged.

## [0.6.2] - 2026-04-30

### Fixed

- Installer now registers the `odoo-mcp` CLI on `PATH`. Previously
  `scripts/install.sh` only ran `uv sync`, which created a virtualenv
  inside the project but did not expose the entry point globally. Users
  following the ONBOARDING guide hit `command not found: odoo-mcp` for
  every CLI command (doctor, status, audit, update, setup --rotate-key).
  The installer now runs `uv tool install --editable . --force` after
  `uv sync` and prints a one-liner to add `~/.local/bin` to `PATH` if
  it is missing. `odoo-mcp update` performs the same step after a
  successful `git pull`, so existing installs converge on the next
  update without requiring a re-install.

## [0.6.1] - 2026-04-28

### Fixed

- Release workflow no longer fails when build-provenance attestations
  cannot be generated. Private repos on free-tier GitHub orgs hit a
  permissions error on `actions/attest-build-provenance`; the step now
  uses `continue-on-error: true`, so the wheel + sdist still publish.
  Once the org upgrades or the repo flips public, attestations resume
  automatically. `odoo-mcp update` already handles "no attestation
  found" as a soft environmental case.

## [0.6.0] - 2026-04-22

### Added

- **Persistent `fields_get` cache (L2).** A SQLite-backed cache at
  `~/.odoo-mcp/fields-cache.db` (chmod 600) survives MCP process
  restarts, so the common "Claude restarts and re-asks for `res.partner`
  fields" path no longer pays the round-trip tax every time. The
  in-memory L1 cache on `OdooClient` is unchanged; the L2 sits behind
  it. Default TTL is 24h per entry. The cache stores only metadata
  (field types, labels, help text) — no record values pass through.
  Configurable via `fields_cache_path` in `[defaults]`; set to `""` to
  disable entirely (L1 still applies).
- **`odoo-mcp cache` CLI.** `odoo-mcp cache --info` prints row count,
  file size, and oldest / newest entry timestamps. `odoo-mcp cache
  --clear` drops everything; `--clear --instance NAME` drops one
  instance's rows. Useful after a model schema change in Odoo.
- **`odoo_lookup` tool.** Fast name-based lookup that runs
  `name ilike <query>` and returns only `id` + `display_name`. Much
  cheaper than `odoo_search_read` for the common "find partner X"
  pattern. The domain shape is fixed, so the domain sandbox is
  intentionally bypassed; sensitive-field redaction still applies.
  Default limit 10, clamped to the instance's `max_records_hard_cap`.

### Security

- GitHub Actions Build Provenance Attestations are now generated for
  every release artifact (`*.whl` and `*.tar.gz`) via Sigstore and
  published to GitHub's transparency log. End users can verify a
  downloaded release tarball with
  `gh attestation verify --owner deltix-consulting --signer-workflow ".github/workflows/release.yml" odoo_mcp-X.Y.Z.tar.gz`.
- `odoo-mcp update` now downloads the latest release tarball and
  verifies its attestation against our release workflow before
  applying. A hard verification failure (signature mismatch, wrong
  signer workflow) refuses the update with a red error. Environmental
  issues (no `gh` on PATH, offline, GitHub down) print a yellow warning
  and prompt the user to confirm.
- New `--skip-verification` flag on `odoo-mcp update` for users who
  explicitly want to bypass the attestation check (not recommended).

## [0.5.0] - 2026-04-22

### Changed

- **BREAKING for fresh prod installs that use admin keys.** Production
  instances now refuse to authenticate when the API key belongs to the
  Odoo superuser (`uid=1`) or any member of `base.group_system`. The
  detection itself shipped in 0.4.1 as an informational warning; in
  0.5.0 it becomes a hard refusal because admin credentials bypass the
  per-user record rules the MCP relies on for ACL scoping. To fix:
  create a dedicated non-admin Odoo user, give it only the groups it
  needs, generate a fresh API key as that user, and run
  `odoo-mcp setup --rotate-key NAME`. Existing setups that knowingly
  need admin keys (e.g. integration test rigs) can opt out by setting
  `refuse_admin_on_production = false` per instance in `config.toml`.

### Added

- New per-instance config keys:
  - `refuse_admin_on_production` (bool, default `true`) — see above.
  - `custom_sensitive_field_patterns` (list of regex strings, default
    empty) — extra always-redacted patterns scoped to one instance.
    Useful for custom-module fields like `my_module\.\w+_secret`. Bad
    regex surface as a `ConfigError` at startup with the offending
    pattern in the message.
  - `max_commits_per_unlock` (int, default `10`, range 1..1000) — caps
    the number of real commits per unlock window. Dry-runs do not count.
- The `odoo_enable_prod_writes` response now includes
  `commits_remaining`, and every commit response (`odoo_create`,
  `odoo_write`, `odoo_archive_or_delete`) on a production instance also
  includes `commits_remaining` so Claude can see the running budget.
- `odoo-mcp status` shows `N commits remaining` next to the unlock
  expiry when a production instance is unlocked.

### Security

- Broader hard-coded always-redacted patterns. In addition to the
  existing password / API key / token / secret families, the redactor
  now blocks fields whose name contains `salary`, `compensation`,
  `payroll`, or `bonus` anywhere; fields named exactly
  `commission_amount`, `nda_text`, `confidential`, or `private_key`;
  and fields matching `\w+_passphrase` or `\w+_credentials`. These are
  always-redacted, not default-hidden — they cannot be opted into via
  `allow_sensitive_fields`.
- Burst-limit gate on production commits (see `max_commits_per_unlock`
  above). Stops a runaway loop from exhausting the 15-minute unlock
  window with hundreds of commits before the operator notices.

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
