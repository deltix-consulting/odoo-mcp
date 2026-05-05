# Changelog

All notable changes to `odoo-mcp` will be documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While we are still on 0.x the public API (tool names, argument shapes,
config schema) may change between minor versions; we will call out any
breaking change explicitly in this file.

## [Unreleased]

<!-- Add new entries here. -->

## [0.12.0] - 2026-05-05

First-time onboarding pass for public users. Someone who finds the GitHub
repo and runs `install.sh` should be able to get a working Odoo connection
without consultant hand-holding.

### Added

- **`odoo-mcp onboarding` command.** Single guided flow that wraps setup
  wizard → doctor → scan-custom. On a fresh machine it prompts for URL /
  database / username / API key, registers in Claude Desktop, runs doctor,
  scans the instance, and writes a paste-ready `~/.odoo-mcp/suggestions.toml`
  (chmod 600). On a machine that already has a config it asks whether to
  add a new instance or just re-scan the existing primary. Doctor failures
  abort before the scan with a clear "fix this and re-run" message. New
  module `src/odoo_mcp/onboarding_cli.py`; new tests in
  `tests/test_onboarding_cli.py`.
- **README "Quick start" section.** Three-step onboarding for end users
  (generate API key, run installer, restart Cowork) at the top of the
  README, before the consultant-facing reference material.
- **"What we never see" privacy section.** Explicit, blunt statement of
  what the MCP does and does not transmit. Added to both README and
  SECURITY.md so each audience sees it.

## [0.11.0] - 2026-05-04

Token-budget pass on every tool response. Our MCP doesn't make LLM calls
itself, but every byte of a tool response is a byte Claude pays for on the
next turn. This release tightens five hot-path payloads and adds a
regression-guarding test file (`tests/test_token_budgets.py`).

### Changed

- **`odoo_describe_model` defaults to a minimal field shape.** Each field
  is now `{type, string, required?, _sensitive?}` only — `help`, `relation`,
  `readonly`, and `_note` are omitted. Pass `verbose=true` to get the full
  schema (the v0.10 shape). Measured: a 280-field synthetic model drops
  from ~120k chars (now `verbose=true`) to ~14k chars (default) — about
  **88% smaller**.
- **`odoo_search_read` and `odoo_read` strip Odoo extras.** Records now
  contain ONLY the fields the caller explicitly requested (plus `id`,
  always preserved as the record key). Odoo's automatic `__last_update`
  and `display_name` extras are dropped unless the caller asked for them.
  Measured: ~75% reduction on a 30-record fixture asking for 2 fields.
- **`odoo_read_group` drops `__domain` by default.** The per-group
  drill-down domain is rarely used by Claude directly. Pass
  `include_domain=true` to keep it. `__count` and `__fold` are still
  returned (tiny + informative). Measured: ~35% reduction on a typical
  20-group response.
- **`odoo_help` defaults to a terse summary + tool one-liners.** The full
  cookbook (common patterns with examples, gotchas) is gated behind
  `verbose=true`. The terse summary points at `verbose=true` for callers
  who want it. Measured: ~45% smaller on the synthetic single-instance
  fixture.
- **Error envelope dedupes hint when it is a substring of error.** If
  `error` already contains the actionable next step, `hint` is dropped
  to avoid duplicated text on the wire.

### Backward compatibility

Opting back in to the v0.10 shape is a single boolean per call:
`verbose=true` on `odoo_help` and `odoo_describe_model`,
`include_domain=true` on `odoo_read_group`. With those flags set, the
response shape is identical to v0.10. `odoo_search_read` and `odoo_read`
have no opt-out — the dropped fields were never explicitly requested by
the caller, so the change is observable only on consumers that relied on
Odoo's incidental extras.

### Added

- `tests/test_token_budgets.py` — guards each of the five reductions with
  a deterministic mock and a hard char-count budget.

## [0.10.0] - 2026-05-04

Per-klant custom-surface scanner. Every klant deployment has Studio fields
and bespoke modules that we cannot anticipate from the v0.9 audit alone.
This release ships an admin CLI that connects to the klant's Odoo,
inventories everything that is NOT part of the embedded Odoo Community 18.0
reference, classifies each finding on sensitivity (with Dutch / Flemish
keyword coverage for BE klanten), and emits either a human-readable
report, a paste-ready TOML config snippet, or machine-readable JSON.

### Added

- **`odoo-mcp scan-custom INSTANCE`** — new admin command. Variants:
  - default: human-readable report (custom models, custom fields on
    standard models, summary counts, sensitivity verdict per field).
  - `--toml`: emits a `[instances.<NAME>]` block ready to paste into
    `~/.odoo-mcp/config.toml`, populating `custom_sensitive_field_patterns`
    and `sensitive_fields`.
  - `--json`: machine-readable JSON for scripting / CI.
- Embedded reference data at `src/odoo_mcp/_odoo_reference.py` — 954
  Odoo Community 18.0 standard models with their declared fields.
  Generated by `scripts/regen_odoo_reference.py` (committed); operators
  rerun once per Odoo major version.
- Sensitivity heuristics module `src/odoo_mcp/_scan_heuristics.py` with
  Dutch / Flemish-aware keyword sets (`loon`, `geboorte`, `vertrouwelijk`,
  `persoonlijk`, `rijksregister`, `geslacht`, `burgerlijk`, `btw`).
  Verdicts: BLOCKED / GATED / LIKELY_SENSITIVE / LIKELY_FINANCIAL /
  BINARY_AUTO_STRIPPED / UNCERTAIN.
- 40 new tests (`tests/test_scan_heuristics.py`,
  `tests/test_scan_cli.py`); total 407 + 2 skipped.

### Notes

- The scan command **deliberately bypasses the dispatcher denylist**.
  This is admin tooling operated by the consultant, not a Claude tool
  call. The denylist exists to constrain Claude, not the operator.
- The scan only reads schema (`ir.model`, `fields_get`). It never reads
  record contents and never prints credential values.
- See `INDUSTRY_AUDIT.md` "Per-klant scan" section for the rerun
  playbook when Odoo bumps its major version.

## [0.9.0] - 2026-05-04

Evidence-based security update. We did a full survey of Odoo Community
18.0's standard-module surface (`addons/` + `odoo/addons/`, 1444 model
declarations) and used the findings to expand `MODEL_DENYLIST` and the
per-model `_DEFAULT_HIDDEN` map. Each addition is cited against a real
file path in the Odoo source — see `INDUSTRY_AUDIT.md` for the full
methodology and citation table. Limitations: Odoo Enterprise modules
(payroll, sign, documents, country-specific payroll packs) are not in
the public repo and remain a per-klant audit responsibility.

### Security

- **`MODEL_DENYLIST` grew from 26 to 66 entries.** New blocked
  categories: WebAuthn / passkey credentials (`auth.passkey.key`),
  2FA telemetry (`auth.totp.rate.limit.log`), per-user OAuth-token
  storage (`res.users.settings`, `res.users.settings.volumes`),
  user-deletion queue (`res.users.deletion`), all of Odoo's mail-server
  credential models (`ir.mail_server`, `fetchmail.server`, the
  `google.gmail.mixin` / `microsoft.outlook.mixin` token mixins,
  `google.service` / `microsoft.service` / `*.calendar.sync`),
  IAP account tokens (`iap.account`, `iap.service`), the entire
  payment-provider surface (`payment.token`, `payment.transaction`,
  `payment.provider`, `payment.method`), additional system internals
  (`ir.default`, `ir.filters`, `ir.actions.act_url`, `ir.actions.todo`,
  `ir.embedded.actions`, `ir.asset`, `ir.profile`, `ir.cron.progress`,
  `ir.cron.trigger`, `ir.module.category`, `ir.model.fields.selection`,
  `ir.model.constraint`, `ir.model.relation`, `ir.model.inherit`,
  `ir.exports`, `ir.exports.line`, `bus.bus`, `bus.presence`), and
  the corrected spelling `auth.oauth.provider` (the existing entry
  with an underscore was a typo and is kept for defense in depth).
- **`_DEFAULT_HIDDEN` got ~50 new (model, field) entries.**
  `hr.employee` gained `passport_id`, `sinid`, `permit_no`, `visa_no`,
  `visa_expire`, the full `private_*` address block, `gender`,
  `emergency_contact` / `emergency_phone`, `study_field` /
  `study_school`, `km_home_work`, `bank_account_id`,
  `private_car_plate`, `barcode`. New per-model entries on
  `hr.contract` (`wage`, `contract_wage`, `notes` — wage is not caught
  by the always-redacted regex), `hr.applicant` / `hr.candidate`
  (recruitment contact), `hr.leave` (`private_name`, `notes`),
  `hr.expense` (`description`), `fleet.vehicle` (`license_plate`,
  `vin_sn`, `description`), `res.partner.bank` (`acc_number`),
  `account.journal` (`bank_acc_number`), `account.payment` (`memo`),
  `calendar.event` (`videocall_location`, `access_token`),
  `calendar.attendee` (`access_token`). `res.partner` gained `comment`
  and `barcode`. The pinning regression test
  (`test_denylist_contents_are_locked_in`) was extended to cover every
  new entry, plus a parametrised test enumerates the new
  `_DEFAULT_HIDDEN` additions so a future refactor that drops one
  trips CI before merge.

### Added

- **Per-industry config templates under `templates/`.** Four starting-
  point TOML files for the typical klant categories deltix sees:
  `wholesale.toml` (stock + purchase + sale + accounting),
  `manufacturing.toml` (mrp + quality + maintenance),
  `hr.toml` (full HR redaction + Enterprise-payroll regex hooks),
  `professional-services.toml` (project + timesheet + helpdesk +
  invoicing — matches deltix's own profile). Each template is a valid
  TOML `[instances.NAME]` block that `load_config` accepts; klant
  admins copy and adapt. README points operators at the directory.
- **`INDUSTRY_AUDIT.md`** at the repo root. Full methodology, citation
  tables for every denylist and default-hidden addition, per-industry
  guidance, an explicit "what was left alone and why" section, and a
  rerun playbook for when Odoo ships a new major version. This is a
  consultant-facing reference, not a runtime artifact.

## [0.8.0] - 2026-04-30

A batch of medium- and low-severity audit-report fixes. No behaviour change
for callers exercising the documented happy paths; several defensive checks
tightened, one performance improvement on the error path, two documentation
corrections.

### Fixed

Security hardening:

- **Confirmation tokens no longer appear in error messages.**
  `ProdGuard.consume_pending` previously echoed the supplied token literal
  back into `ProdGuardError`, which the dispatcher then included in the
  audit log via `_args_shape` -> `details.error`. Tokens are short-lived
  but audit logs retain for 30 days. The error now refers to "the
  supplied confirmation token" without the literal value.
- **Audit log rotates mid-flight, not just at startup.** `AuditLog.log`
  now compares today's UTC date against a cached last-rotation date on
  every write; a long-running MCP that crosses midnight rotates
  yesterday's `audit.jsonl` into a dated file before appending the new
  day's events. The check is one date comparison on the hot path; only
  on a date change do we stat the file and rename.
- **`launch-env` now refuses loose config-file permissions.** The
  `launch` subcommand pulls credentials from Keychain and injects them
  into `os.environ` before `build_app` performs the standard
  `_check_file_permissions` gate. `_collect_launch_env` now applies the
  same gate up front, so a 0o644 config aborts before any Keychain
  access.
- **TOML writer now escapes `\r`.** The wizard's `_toml_value` previously
  escaped `\\`, `"`, `\n`, and `\t` but a pasted CRLF could leak a
  literal carriage return into `config.toml`, which `tomllib` parsed
  oddly. Now escaped consistently with the other whitespace forms.
- **Stricter `offset` validation.** `_offset` previously accepted any
  truthy value coercible to `int` (strings, floats), inconsistent with
  the strict `_require_str` / `_require_list_of_int` checks elsewhere.
  A new `_require_int_or_default` helper now rejects non-int (and
  rejects bool, which is a subclass of int).

Defensive code:

- **`redact_response` drops fields with no type info.** If a returned
  record contains a field name that is not in the `fields_get` result,
  `redact_response` previously passed it through unredacted; for an
  unannotated binary blob, that defeated the include-binary policy.
  We now drop such fields entirely as defense in depth — the dispatcher
  validates the requested field list against `fields_get` upstream, so
  in practice this branch never fires for normal records.
- **Attestation filename pinned by test.** Added
  `tests/test_attestation_filename_pinning.py` to assert that the URL
  built by `attestation._tarball_url` matches `pyproject.toml`'s
  package name (with pip/uv's hyphen-to-underscore normalization). A
  silent rename of `project.name` would otherwise break
  `odoo-mcp update` by downloading the wrong file.

Performance:

- **Error-message redaction is now O(input) per call.** The `_SECRETS`
  registry is bounded at 64 entries (LRU-evicted) and a single
  compiled-regex alternation replaces the previous O(n_secrets) substring
  loop. The pattern is rebuilt lazily on registry change.

Audit log accuracy:

- **`odoo_help` and `odoo_list_instances` now use proper op tags.**
  Both previously logged `op=fields_get`, which was misleading. New
  `Operation.HELP` and `Operation.LIST_INSTANCES` are added to the read
  ops set; the audit log now records them under their own tag.

### Changed

- **Documentation: tool count and operation count corrected.** README
  previously claimed "ten well-defined tools" and "the seven allowed
  operations"; these are now twelve and twelve respectively (the help
  tool was missing from the README's tool table; the operation count
  needs to include the new `HELP` / `LIST_INSTANCES` ops as well as
  pre-existing ones the prose had drifted from).
- **SECURITY.md: explicit policy on usernames vs. credentials.** Added
  one paragraph under the threat model clarifying that usernames
  (typically email addresses) are NOT redacted from error messages,
  because losing them would obscure useful diagnostic context. Treated
  as identifying-but-not-secret PII; never written to the audit log,
  never returned in tool responses, but free to appear in operator-facing
  error text.

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
