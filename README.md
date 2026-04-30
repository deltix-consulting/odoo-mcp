# odoo-mcp — Security-first MCP server for Odoo

## What it does

odoo-mcp is a local Model Context Protocol server that exposes a
security-gated slice of [Odoo](https://www.odoo.com) to Claude Desktop
or Claude Code over stdio. It lets Claude search partners, read
invoices, update leads, aggregate pipeline data, and archive or delete
records through twelve well-defined tools — and nothing else. Writes to
production are off by default, every call is rate-limited and audited,
and a hardcoded denylist blocks calls to `res.users`, `ir.*` internals,
stored code (`ir.actions.server`, `mail.template`, `ir.ui.view`),
system configuration (`ir.config_parameter`), raw attachments, and a
handful of other sensitive models — regardless of config.

## Why this exists

Plugging an LLM straight into an XML-RPC endpoint on your Odoo database
is a great way to lose a weekend cleaning up hallucinated `write` calls.
The vanilla Odoo external API has no concept of "read-only mode",
"confirm this before committing", "strip the VAT numbers on the way
out", or "don't let the model call `execute_kw('res.users', 'unlink',
...)` because it read a funny comment somewhere". This server adds that
layer — server-side, not in a tool description Claude is free to ignore
— so you can hand Claude useful Odoo access without handing it the keys
to production.

## Threat model summary

Defended against:

- Accidental destructive writes on production (prompt-injected or
  hallucinated). Writes are blocked by default, require explicit unlock,
  default to dry-run, and commit only on a single-use confirmation token.
- Credential exfiltration through errors, logs, or child processes. API
  keys live in the macOS Keychain, are scrubbed from error messages, and
  never appear in the audit log.
- Field-level PII leakage. Passwords, API keys, and tokens are always
  stripped. VAT, IBAN, SSN, and similar are hidden unless the caller
  opts in per-field.
- Cross-model privilege escalation via domain traversal. Dotted field
  paths like `create_uid.login` are rejected by the domain sandbox.
- Runaway resource consumption. Every call is rate-limited, record
  counts are capped, and each XML-RPC call has a per-instance timeout.
- Arbitrary method execution. There is no `execute_kw` surface — only
  the twelve allowed operations.

Not defended against: a compromised host, a malicious Odoo server, or
side-channel attacks. See `SECURITY.md` for the full threat model.

## Quick install

Prerequisites on the target machine:

- macOS (Keychain integration is macOS-specific)
- Claude Desktop or Claude Code
- [`gh` CLI](https://cli.github.com) authenticated against GitHub
  (`brew install gh && gh auth login`) — required because this repo is
  private
- An Odoo user with an API key (Settings → Users → API Keys — do not
  use your password)

### Via installer (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/deltix-consulting/odoo-mcp/main/scripts/install.sh | bash
```

The installer checks for `uv` (and installs it via the official
installer if missing), confirms `gh` is authenticated, downloads the
latest release tarball into `~/odoo-mcp`, **verifies the build-provenance
attestation** before extracting, runs `uv sync`, and hands off to
`odoo-mcp setup`. Set `ODOO_MCP_HOME` to override the install
directory, pass `--git` to clone `main` instead of using a tagged
release, or pass `--skip-verification` for environmental edge cases
where attestation verification cannot run (offline, free-tier org with
attestations disabled). Hard verification failures abort the install.

Since v0.6.0, every release artifact (`*.whl` and `*.tar.gz`) is signed
via Sigstore using GitHub Actions Build Provenance Attestations. You
can also verify a downloaded tarball manually with
`gh attestation verify` — see the "Verifying releases" section in
[`SECURITY.md`](SECURITY.md).

### Via git clone (manual)

```bash
git clone https://github.com/deltix-consulting/odoo-mcp.git
cd odoo-mcp
uv sync
uv run odoo-mcp setup
```

The wizard prompts for URL, database, username, API key, and
production status; stores credentials in the Keychain; generates
`~/.odoo-mcp/config.toml` (chmod 600) and `~/.odoo-mcp/launch.sh`;
registers the MCP in Claude Desktop; and runs `doctor` to confirm
everything works. Restart Claude Desktop and you're done.

## Updating

Once installed, pull the latest version and re-sync dependencies with:

```bash
uv run odoo-mcp update
```

This runs `git pull` against the install directory followed by
`uv sync`. It does not touch your `~/.odoo-mcp/config.toml`,
Keychain entries, or Claude Desktop registration.

Since v0.7.0 the launcher template uses a single Python process to load
Keychain credentials and start the server, instead of two `uv run`
invocations. This shaves roughly 150-300 ms off every Claude Cowork /
Claude Desktop launch. Existing installs auto-migrate to the new
template on the next `odoo-mcp update`; the legacy `launch-env`
subcommand stays for backward compatibility.

## Configuration

The wizard generates a config that looks like this at
`~/.odoo-mcp/config.toml`:

```toml
[defaults]
timeout_seconds = 30
max_records_default = 50
max_records_hard_cap = 500
# Default since v0.4.0: open mode. Every non-denylisted Odoo model is
# reachable. Replace with an explicit list to switch to strict mode
# (globally here, or per instance under [instances.NAME]).
allowed_models = ["*"]

[instances.prod]
url = "https://your-odoo.com"
database = "prod_db"
credentials_env_prefix = "ODOO_MCP_PROD"
production = true
```

Fields:

- `timeout_seconds` — per-call XML-RPC timeout.
- `max_records_default` — default cap when a tool call omits `limit`.
- `max_records_hard_cap` — absolute ceiling; `limit` is always clamped
  to this regardless of what the caller asks for.
- `allowed_models` — controls which models tool calls may target.
  Two modes:
  - **Open mode** (default): `allowed_models = ["*"]`. Every Odoo
    model is reachable except those on the hardcoded denylist
    (see "Allowed models" below). This is the mode a fresh install
    ships with.
  - **Strict mode**: `allowed_models = ["res.partner", "crm.lead", ...]`.
    Only the enumerated models are reachable. The denylist still
    applies.
  Per-instance overrides are supported under
  `[instances.NAME]` with an `allowed_models = [...]` key —
  e.g. prod in strict mode, dev in open mode.
- `url` — must be HTTPS on any instance marked `production = true`.
- `database` — the Odoo database name.
- `credentials_env_prefix` — prefix under which the launcher exports
  `<PREFIX>_USERNAME` and `<PREFIX>_API_KEY` from the Keychain.
- `production` — flips the instance into read-only-by-default mode
  with the full write-unlock workflow.

The config file must be `chmod 600`. The server refuses to start
otherwise.

## Credentials

Credentials never touch disk in plaintext. The setup wizard stores your
Odoo username and API key in the macOS Keychain under an account named
`odoo-mcp-<instance>`. At launch, `launch.sh` calls
`odoo-mcp launch-env`, which reads the Keychain and emits `export`
lines; those get `eval`'d into the server's environment. The server
reads them, authenticates, and then deletes them from `os.environ` so
they don't leak into subprocess environments or error traces.

To rotate an API key:

```bash
uv run odoo-mcp setup --rotate-key NAME
```

This prompts for the new key, overwrites the Keychain entry, and
leaves everything else untouched.

## Available tools

| Tool | Purpose | Writes | Prod-gated |
|---|---|---|---|
| `odoo_help` | Capability overview (no Odoo round-trip) | no | — |
| `odoo_list_instances` | List configured instances and their state | no | — |
| `odoo_describe_model` | Field metadata for one allowlisted model | no | — |
| `odoo_lookup` | Fast `name ilike` lookup, returns id + display_name | no | — |
| `odoo_search_read` | Query records (explicit fields, sandboxed domain) | no | — |
| `odoo_search_count` | Count records matching a domain | no | — |
| `odoo_read_group` | Aggregate (sum/avg/count/min/max) with groupby | no | — |
| `odoo_read` | Read specific records by ID | no | — |
| `odoo_create` | Create a record | yes | yes |
| `odoo_write` | Update records | yes | yes |
| `odoo_archive_or_delete` | Archive (`active=False`) or permanently `unlink` records | yes | yes |
| `odoo_enable_prod_writes` | Unlock prod writes for 15 minutes | — | yes |

No direct `execute_kw`. No workflow buttons. No `copy`, `name_search`,
`fields_view_get`. `unlink` is only reachable through
`odoo_archive_or_delete`, which forces an explicit `mode` choice and
goes through the full prod-guard + dry-run + confirmation-token flow.

## CLI commands

| Command | What it does |
|---|---|
| `odoo-mcp setup` | First-time wizard: config, credentials, launcher, Claude Desktop registration |
| `odoo-mcp setup --add` | Add another instance to an existing config |
| `odoo-mcp setup --remove` | Remove an instance and its Keychain entries |
| `odoo-mcp setup --list` | List configured instances |
| `odoo-mcp setup --rotate-key NAME` | Rotate the API key for one instance |
| `odoo-mcp setup --regenerate-launcher` | Rewrite `launch.sh` (useful after moving the repo) |
| `odoo-mcp uninstall` | Remove config, Keychain entries, launcher, Claude Desktop registration, and the `uv tool` install (project checkout left alone) |
| `odoo-mcp doctor` | Pre-flight: config perms, audit log, TLS, auth, smoke call |
| `odoo-mcp status` | Live status: which instances are authenticated, unlock state, rate-limit budget |
| `odoo-mcp audit` | Audit log inspector: filter by instance, tool, date, result |
| `odoo-mcp cache --info` | Show persistent fields-cache stats (rows, size, age) |
| `odoo-mcp cache --clear` | Drop persistent fields-cache rows (`--instance NAME` to scope) |

## Production write workflow

Writes against any instance with `production = true` go through three
gates. Each gate is independent and server-enforced; bypassing any one
of them requires local code changes, not a clever prompt.

1. **Unlock.** Call `odoo_enable_prod_writes` with the instance name.
   This flips a 15-minute activity-based flag on the server. Every
   write call after this point refreshes the expiry; 15 minutes of
   silence relocks.

2. **Dry-run preview.** The first real write call defaults to
   `dry_run=true` on prod. The server validates the payload, returns a
   preview of what would change, and issues a single-use
   `confirmation_token` bound to `(instance, operation, model)`.

3. **Commit.** Call the same tool again with `dry_run=false` and the
   `confirmation_token` from step 2. The server consumes the token and
   actually commits to Odoo. The token is single-use: a second commit
   needs a second dry run.

Example:

```
# 1. Unlock
odoo_enable_prod_writes(instance="prod")

# 2. Dry run — returns { preview: true, confirmation_token: "abc123", ... }
odoo_write(instance="prod", model="crm.lead", ids=[42],
           values={"stage_id": 3})

# 3. Commit — passes the token from step 2
odoo_write(instance="prod", model="crm.lead", ids=[42],
           values={"stage_id": 3},
           dry_run=false, confirmation_token="abc123")
```

On non-production instances, writes commit directly (no unlock, no
dry-run default, no token). The workflow exists specifically to put
friction between Claude and prod data.

Since v0.5.0 the unlock window also enforces a burst limit: by default
at most 10 real commits per unlock. Dry-runs do not count, and the
remaining budget is surfaced on every commit response and on
`odoo-mcp status`. Tune per instance with `max_commits_per_unlock = N`
(range 1..1000). When the budget hits zero the operator must call
`odoo_enable_prod_writes` again to renew, which is a hard checkpoint
against runaway loops.

Also new in v0.5.0: production instances refuse to authenticate with
Odoo admin credentials (uid=1 or `base.group_system`). Admin keys
bypass per-user record rules, which removes the ACL scoping the MCP
relies on. Create a dedicated non-admin user instead, or — only if you
truly know what you're doing — set
`refuse_admin_on_production = false` on the instance to opt out.

## Sensitive fields

Two categories of field-level protection:

**Always redacted.** Matched by regex on the field name, not a fixed
list, so a new module's `my_module_api_key` field is caught by default.
Never returned, regardless of `allow_sensitive_fields`. Also cannot be
written, so a compromised session can't plant an API key or reset a
password. The patterns cover `password`, `*_password`, `password_crypt`,
`new_password`, `api_key`, `*_api_key`, `token`, `*_token`,
`access_token`, `refresh_token`, and `*_secret`. Since v0.5.0 the
patterns also cover any field whose name contains `salary`,
`compensation`, `payroll`, or `bonus`; fields named exactly
`commission_amount`, `nda_text`, `confidential`, or `private_key`; and
any `*_passphrase` or `*_credentials` field. Each instance can extend
this list with `custom_sensitive_field_patterns = ["..."]` in
`config.toml` for custom-module fields the built-in list doesn't cover.

**Default hidden.** Per-model sensitive fields that require per-call
opt-in via `allow_sensitive_fields=[...]`. Currently:

- `res.partner`: `vat`, `bank_ids`, `company_registry`
- `account.payment`: `partner_bank_id`
- `hr.employee`: `ssnid`, `identification_id`, `private_email`,
  `private_phone`, `birthday`, `marital`, `children`,
  `spouse_complete_name`, `spouse_birthdate`, `country_of_birth`,
  `place_of_birth`

Default-hidden fields can be written without opt-in (you might
legitimately need to update a VAT number) but cannot be read back
without explicit unlock. This matters because the user reviews tool
call arguments before approving them, so the opt-in is visible.

Default-hidden fields also cannot be used as a `groupby` dimension in
`odoo_read_group` without opt-in, because grouping effectively reveals
the distinct values.

## Allowed models

Since v0.4.0, the default is **open mode**: every Odoo model is
reachable through the tools, *except* those on the hardcoded
`MODEL_DENYLIST`. The denylist cannot be disabled through config —
it is a safety invariant — and covers:

- Auth / user / group tables: `res.users`, `res.users.apikeys`,
  `res.groups`, `auth_totp.device`, `auth_oauth.provider`,
  `auth_signup.reset.password`, plus related log / identity-check /
  description tables.
- ACL and rule definitions: `ir.model.access`, `ir.rule`.
- Stored executable content (code and template injection vectors):
  `ir.actions.server`, `ir.actions.client`, `ir.ui.view`,
  `mail.template`.
- System configuration: `ir.config_parameter` (often holds external
  integration secrets).
- Scheduler / module / logging internals: `ir.cron`,
  `ir.module.module`, `ir.logging`, `ir.sequence`.
- Model metadata: `ir.model`, `ir.model.fields`, `ir.model.data`.
- Raw attachments: `ir.attachment` (references any `res_model`;
  opt in per-instance via a strict allowlist if you specifically
  need it).
- Import/export infrastructure: `base_import.import`,
  `base_import.mapping`.

Calls to any denylisted model are rejected with `model_not_allowed`
before any XML-RPC happens.

To switch an instance into **strict mode**, put an explicit list in
the config — either globally under `[defaults]` or per-instance:

```toml
[instances.prod]
# ...
allowed_models = [
    "res.partner", "crm.lead", "sale.order", "account.move",
]
```

Strict mode is a complete override: the list replaces the default,
include the full set you want. The denylist still applies on top of
a strict list, so you cannot re-enable `res.users` or friends by
putting them on it.

Use `odoo_list_instances` to see the `allowlist_mode` (`"open"` or
`"strict"`) per instance at runtime.

## Audit log

Every tool call (success or failure) writes one JSONL line to
`~/.odoo-mcp/audit.jsonl`. The file rotates daily and retains 30 days
of history. Each line contains:

- Timestamp, MCP version, PID
- Instance, tool, operation, model
- Result code (`ok` or an error code)
- Record count, field count, duration in ms
- Whether the call was a dry run

The audit log never contains field values, domain operands, credentials,
or record content. It is safe to ship to a SIEM.

If the audit log becomes unwritable (permissions, disk full), the
server fails closed: every tool call is refused until the log is
writable again.

Use `odoo-mcp audit` to query the log interactively.

## Caching

`fields_get` (the Odoo metadata call that the MCP uses to validate
domains and field lists) is cached at two levels:

- **L1, in-memory.** Per-process dict on `OdooClient`. No I/O, never
  expires within a process lifetime.
- **L2, persistent.** SQLite database at `~/.odoo-mcp/fields-cache.db`
  (chmod 600), shared across MCP process restarts. Default TTL is
  24 hours per `(instance, model)` row. Only field metadata (types,
  labels, help text) is stored — never record values.

The L2 cache is populated lazily on first use of each model and is
opt-out: set `fields_cache_path = ""` in `[defaults]` to disable it
entirely (the L1 cache still applies). After a schema change in Odoo
(new module, new custom field), use `odoo-mcp cache --clear` to drop
stale entries:

```bash
odoo-mcp cache --info                        # row count, size, ages
odoo-mcp cache --clear                       # drop everything
odoo-mcp cache --clear --instance prod       # drop one instance only
```

## Development

```bash
git clone https://github.com/deltix-consulting/odoo-mcp.git
cd odoo-mcp
uv sync --extra dev

uv run pytest -q                     # unit tests
uv run ruff check src tests          # lint
uv run ruff format --check src tests # format check
uv run mypy src                      # strict type check
```

Integration tests against a live Odoo instance:

```bash
export ODOO_MCP_TEST_INSTANCE=dev
export ODOO_MCP_TEST_URL="https://dev.your-odoo.com"
export ODOO_MCP_TEST_DB="dev_db"
export ODOO_MCP_TEST_USERNAME="..."
export ODOO_MCP_TEST_API_KEY="..."
uv run pytest -m integration
```

CI runs ruff, ruff format, mypy strict, and pytest on every push.

To run the same lint/format/type checks automatically before each commit,
install the pre-commit hooks once:

```bash
uv sync --extra dev
uv run pre-commit install
```

The hooks run `ruff check --fix`, `ruff format`, `mypy --strict`, plus a few
standard whitespace/yaml sanity checks. Pytest is intentionally left out —
too slow for a pre-commit hook, and CI runs it anyway.

### Debug logging

The MCP stdio channel carries protocol traffic, so the server is silent by
default. To stream structured diagnostics to stderr, export
`ODOO_MCP_LOG_LEVEL=DEBUG` (or `INFO` / `WARNING` / `ERROR`) before starting
the process. Registered credentials are automatically scrubbed from log
output. Unset or `OFF` disables logging entirely.

## Security reporting

See [`SECURITY.md`](SECURITY.md) for the threat model and the
vulnerability disclosure process. Short version: email
`security@deltix.pro` and expect a response within five business days.

## License

Proprietary. Copyright deltix consulting. Not for redistribution.
