# Odoo MCP

A **security-first** Model Context Protocol server that exposes a tightly-scoped
slice of [Odoo](https://www.odoo.com) to Claude Code over local stdio.

It lets Claude help you work with Odoo — searching partners, updating leads,
creating tasks — without giving it the keys to the kingdom. Writes to
production are off by default, every call is rate-limited and audited, and no
operation ever touches `res.users`, `ir.*`, or any model outside an explicit
allowlist.

## Security posture

What the MCP **can** do:

- `search_read`, `read`, `create`, `write` on an explicit allowlist of models
- `fields_get` (via the `odoo_describe_model` tool) for discovery

What the MCP **cannot** do, ever:

- `unlink` (delete records)
- `execute_kw` arbitrary methods (no workflow buttons, no custom methods, no
  `action_confirm` / `button_cancel` / etc.)
- Touch `res.users`, `ir.model`, `ir.config_parameter`, or anything outside
  the allowlist
- Read password hashes, API keys, tokens, or any field matching
  `*_password`, `*_secret`, `*_api_key`, `*_token` — these are dropped on
  the way out regardless of what Claude asks for
- Write those same protected fields — so even a compromised Claude session
  can't reset a password or plant an API key
- Read VAT / IBAN / employee PII without per-call explicit opt-in
- Traverse dotted fields in domains (e.g. `create_uid.login` → `res.users`)
- Run without an audit log

What **production** specifically adds:

1. Writes are blocked by default — call `odoo_enable_prod_writes` to unlock.
2. Even when unlocked, writes default to `dry_run=true` — you get a preview
   and a confirmation token, not a commit.
3. The real commit needs `dry_run=false` AND the confirmation token from
   step 2.
4. Every prod call is audited. Unwritable audit log = hard refusal to run.
5. TLS is strict (verified cert, no self-signed).
6. Unlock auto-relocks after 15 minutes of inactivity.

## Install

This repo uses [`uv`](https://github.com/astral-sh/uv):

```bash
cd "odoo MCP"
uv sync
```

## Configure

Create `~/.odoo-mcp/config.toml`:

```bash
mkdir -p ~/.odoo-mcp
cat > ~/.odoo-mcp/config.toml <<'EOF'
[defaults]
timeout_seconds = 30
max_records_default = 50
max_records_hard_cap = 500

[instances.dev]
url = "https://dev.your-odoo.com"
database = "dev_db"
credentials_env_prefix = "ODOO_MCP_DEV"
production = false

[instances.prod]
url = "https://your-odoo.com"
database = "prod_db"
credentials_env_prefix = "ODOO_MCP_PROD"
production = true
EOF
chmod 600 ~/.odoo-mcp/config.toml
```

The config file **must** be `chmod 600`. The MCP refuses to start otherwise.

Set credentials via environment variables. The MCP reads them at startup and
then **deletes** them from `os.environ` — they never appear in child
processes, error messages, or the audit log:

```bash
export ODOO_MCP_DEV_USERNAME="me@example.com"
export ODOO_MCP_DEV_API_KEY="$(pass odoo/dev/api-key)"   # or any secret manager
export ODOO_MCP_PROD_USERNAME="me@example.com"
export ODOO_MCP_PROD_API_KEY="$(pass odoo/prod/api-key)"
```

> **Use API keys, not passwords.** In Odoo, go to *Settings → Users → API Keys*
> and mint a per-purpose key. API keys are revocable without resetting your
> password.

## Verify the setup

```bash
uv run python -m odoo_mcp doctor
```

The doctor walks every startup step (config perms, TOML parse, audit log,
credential env, TLS, `authenticate`, smoke `fields_get`) and prints a
traffic-light report. Exits non-zero on any red.

## Register with Claude Code

```bash
claude mcp add odoo-mcp -- uv run --directory "$(pwd)" python -m odoo_mcp
```

Then restart Claude Code and run `/mcp` to confirm it's connected.

## Tools exposed

| Tool | Purpose |
|---|---|
| `odoo_list_instances` | List configured instances and their prod status |
| `odoo_describe_model` | Field metadata for an allowlisted model |
| `odoo_search_read` | Query records (explicit field list required, domain sandboxed) |
| `odoo_read` | Read records by ID |
| `odoo_create` | Create a record (gated on prod) |
| `odoo_write` | Update records (gated on prod) |
| `odoo_enable_prod_writes` | Unlock prod writes for 15 minutes of activity |

## Audit log

Every tool call writes one line to `~/.odoo-mcp/audit.jsonl` — success or
failure, rotated daily, 30-day retention. The log contains **metadata only**:
timestamp, instance, tool, model, operation, record counts, duration, and
whether the call was a dry run. It never contains field values, credentials,
or domain operands, so it is safe to keep around.

If the audit log becomes unwritable, the MCP fails closed — no tool calls
will succeed until the log is writable again.

## Development

```bash
uv run pytest -q                    # all unit tests
uv run ruff check src tests          # lint
uv run mypy src                      # type check
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

## Threat model

This MCP is designed under the assumption that **Claude may call any tool
with any arguments**, including arguments it was tricked into using via
prompt injection from observed content. It is not safe to trust that Claude
will "know better" than to call `odoo_create` with attacker-chosen values.
Every guardrail in the pipeline is therefore enforced server-side, not in
the tool description.

Specifically defended against:

1. **Prompt-injected writes on prod** — blocked by the default read-only
   state on prod, the required explicit `odoo_enable_prod_writes` call, and
   the dry-run + confirmation-token dance.
2. **Prompt-injected reads of sensitive fields** — blocked by the
   default-hidden field policy (VAT, IBAN, SSN, etc.) and the always-redacted
   pattern list (passwords, tokens, keys).
3. **Domain-based cross-model traversal** — blocked by the domain sandbox,
   which rejects any dotted field path.
4. **Arbitrary Odoo method calls** — not exposed. No `execute_kw` surface
   exists at the MCP boundary.
5. **Credential exfiltration via errors** — every `OdooMcpError` subclass
   scrubs registered secret values from its string form, including chained
   causes; `Credentials.__repr__` returns a redaction placeholder.
6. **Runaway queries / DoS** — capped record limits (default 50, hard 500)
   and per-instance token-bucket rate limiting.
7. **Log-based leakage** — the audit log records only metadata and is the
   only persistent output of the MCP besides Odoo itself.

Not defended against:

- A compromised host. If an attacker has code execution as your OS user,
  they can read your API keys out of the running process memory. That's
  outside the MCP's trust boundary; use OS-level isolation (e.g. a
  dedicated user, a container) if you need that.
- A malicious Odoo server. The MCP trusts its configured Odoo instance to
  return what it asks for. If the server returns maliciously-crafted
  records, the MCP's redaction layer still strips `password`-named fields
  and strips anything in the default-hidden list, but it cannot inspect
  the semantic content of fields it *does* return.
