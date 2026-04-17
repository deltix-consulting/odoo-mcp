# odoo-mcp — Security-first MCP server for Odoo

## What it does

odoo-mcp is a local Model Context Protocol server that exposes a tightly
scoped slice of [Odoo](https://www.odoo.com) to Claude Desktop or Claude
Code over stdio. It lets Claude search partners, read invoices, update
leads, and aggregate pipeline data through nine well-defined tools — and
nothing else. Writes to production are off by default, every call is
rate-limited and audited, and no operation ever touches `res.users`,
`ir.*`, or any model outside an explicit allowlist.

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
  the seven allowed operations.

Not defended against: a compromised host, a malicious Odoo server, or
side-channel attacks. See `SECURITY.md` for the full threat model.

## Quick install

### 1. Prerequisites

- macOS (Keychain integration is macOS-specific)
- [`uv`](https://github.com/astral-sh/uv)
- Claude Desktop or Claude Code
- An Odoo user with an API key (Settings → Users → API Keys — do not use
  your password)

### 2. Clone and sync

```bash
git clone https://github.com/deltix-consulting/odoo-mcp.git
cd odoo-mcp
uv sync
```

### 3. Run the setup wizard

```bash
uv run odoo-mcp setup
```

The wizard prompts for URL, database, username, API key, and
production status; stores credentials in the Keychain; generates
`~/.odoo-mcp/config.toml` (chmod 600) and `~/.odoo-mcp/launch.sh`;
registers the MCP in Claude Desktop; and runs `doctor` to confirm
everything works. Restart Claude Desktop and you're done.

## Configuration

The wizard generates a config that looks like this at
`~/.odoo-mcp/config.toml`:

```toml
[defaults]
timeout_seconds = 30
max_records_default = 50
max_records_hard_cap = 500
allowed_models = [
    "res.partner", "crm.lead", "crm.team",
    "sale.order", "sale.order.line",
    "product.product", "product.template",
    "account.move", "account.move.line", "account.payment",
    "project.project", "project.task",
    "hr.employee", "hr.leave",
]

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
- `allowed_models` — the only models any tool call is allowed to touch.
  Per-instance overrides are supported under
  `[instances.NAME]` with an `allowed_models = [...]` key.
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
| `odoo_list_instances` | List configured instances and their state | no | — |
| `odoo_describe_model` | Field metadata for one allowlisted model | no | — |
| `odoo_search_read` | Query records (explicit fields, sandboxed domain) | no | — |
| `odoo_search_count` | Count records matching a domain | no | — |
| `odoo_read_group` | Aggregate (sum/avg/count/min/max) with groupby | no | — |
| `odoo_read` | Read specific records by ID | no | — |
| `odoo_create` | Create a record | yes | yes |
| `odoo_write` | Update records | yes | yes |
| `odoo_enable_prod_writes` | Unlock prod writes for 15 minutes | — | yes |

No `unlink`. No `execute_kw`. No workflow buttons. No `copy`,
`name_search`, `fields_view_get`.

## CLI commands

| Command | What it does |
|---|---|
| `odoo-mcp setup` | First-time wizard: config, credentials, launcher, Claude Desktop registration |
| `odoo-mcp setup --add` | Add another instance to an existing config |
| `odoo-mcp setup --remove` | Remove an instance and its Keychain entries |
| `odoo-mcp setup --list` | List configured instances |
| `odoo-mcp setup --rotate-key NAME` | Rotate the API key for one instance |
| `odoo-mcp setup --regenerate-launcher` | Rewrite `launch.sh` (useful after moving the repo) |
| `odoo-mcp doctor` | Pre-flight: config perms, audit log, TLS, auth, smoke call |
| `odoo-mcp status` | Live status: which instances are authenticated, unlock state, rate-limit budget |
| `odoo-mcp audit` | Audit log inspector: filter by instance, tool, date, result |

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

## Sensitive fields

Two categories of field-level protection:

**Always redacted.** Matched by regex on the field name, not a fixed
list, so a new module's `my_module_api_key` field is caught by default.
Never returned, regardless of `allow_sensitive_fields`. Also cannot be
written, so a compromised session can't plant an API key or reset a
password. The patterns cover `password`, `*_password`, `password_crypt`,
`new_password`, `api_key`, `*_api_key`, `token`, `*_token`,
`access_token`, `refresh_token`, and `*_secret`.

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

By default, the allowlist covers the common deltix consulting surface:

```
res.partner
crm.lead
crm.team
sale.order
sale.order.line
product.product
product.template
account.move
account.move.line
account.payment
project.project
project.task
hr.employee
hr.leave
```

Any call referencing a model outside this list is rejected with
`model_not_allowed` before any XML-RPC happens. To extend the list for
one instance, add an `allowed_models = [...]` key under
`[instances.NAME]` in the config — this overrides the default, so
include the full set you want, not just additions.

`res.users`, `ir.model`, `ir.config_parameter`, `ir.module.module`,
and similar are intentionally not on the default list and should stay
that way.

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

## Security reporting

See [`SECURITY.md`](SECURITY.md) for the threat model and the
vulnerability disclosure process. Short version: email
`security@deltix.pro` and expect a response within five business days.

## License

Proprietary. Copyright deltix consulting. Not for redistribution.
