# odoo-mcp

[![CI](https://github.com/deltix-consulting/odoo-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/deltix-consulting/odoo-mcp/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/deltix-consulting/odoo-mcp?display_name=tag&sort=semver)](https://github.com/deltix-consulting/odoo-mcp/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](pyproject.toml)
[![Build provenance attested](https://img.shields.io/badge/build%20provenance-Sigstore-success)](https://docs.github.com/en/actions/security-for-github-actions/using-artifact-attestations)

**Let Claude, Codex, Cursor or any MCP client work directly against your [Odoo](https://www.odoo.com) — without a hallucinating loop wiping production.**

Built by [deltix](https://www.deltix.pro) for in-house Odoo teams and individual operators who want AI on tap during day-to-day work — reading invoices, drafting partners, reviewing pipelines, auditing custom modules — but refuse to expose the database to a misbehaving agent. Local, open source, MIT.

> **One MCP install = one Odoo deployment.** Connect dev, staging, and prod of the same Odoo side by side in one config — that's what the multi-instance support is for. Do not point a single MCP install at multiple unrelated organisations' Odoos; credentials, audit log, and OS keychain entries are shared per process. See [SECURITY.md](SECURITY.md#scope-and-shared-responsibility).

## What you get

- **Production-safe writes.** Every commit on a production instance goes through three independent gates: explicit unlock, dry-run preview, single-use confirmation token. A hallucinated 200-call loop cannot silently mutate data.
- **Per-call PII redaction.** VAT, IBAN, salary, RSZ, employee birth dates and 60+ other patterns are stripped by default. Passwords and API keys can never be read, full stop.
- **Dev + prod side by side.** Wire up `[instances.dev]` and `[instances.prod]` of the same Odoo deployment in one config. Each instance has its own API key, its own rate-limit budget, and its own prod-guard state.
- **Works with every major MCP client.** Claude Desktop, Claude Code, OpenAI Codex, Cursor, Windsurf, Continue.dev, Zed. One server, paste-ready config snippets via `odoo-mcp client-config`.
- **No telemetry, no phone-home, no data exfil path.** The only outbound traffic is to your Odoo. We never see your URL, your queries, your data, or your audit log.
- **66-model security denylist hardcoded.** Auth tables, ACL rules, mail credentials, payment tokens, stored executable content, attachments — unreachable, not config-overridable.
- **Audit log on every call.** One JSONL line per tool call with timing and shape, never values. Fail-closed if the log becomes unwritable.

## Where it fits

- **In-house operators with one Odoo deployment** — reading reports, drafting partners, reviewing pipelines without writing SQL or building Studio dashboards.
- **Dev → prod workflows** — try a write against `[instances.dev]` first, then re-run against `[instances.prod]` with the explicit unlock + token flow.
- **Schema audit and migration prep** — `odoo-mcp scan-custom` diffs your live Odoo against an embedded reference schema (currently Odoo 18 Community) to surface custom models, Studio fields, and likely-sensitive columns before they show up in an incident. On other Odoo versions the diff still runs — fields present in your Odoo but not in the reference get flagged as custom (a small overcount on newer versions, harmless).
- **Read-only demos and training** — set `ODOO_MCP_READ_ONLY=1` and hand a session to anyone safely.

> **As-is software, MIT-licensed.** No external security audit has been performed.
> Review [SECURITY.md](SECURITY.md) before deploying to production. The MCP runs on
> macOS, Windows 10+, and Linux (with libsecret).

## Quick start

> **First time installing?** Read **[GETTING_STARTED.md](GETTING_STARTED.md)** — it walks through prerequisites, picking the right Odoo user, creating an API key step-by-step (and what permissions you need), the install command, and the top ten gotchas. About 5 minutes to read; 10 minutes to install.
>
> **For production: [VERIFY.md](VERIFY.md)** describes the verified-release install flow (download release artifact, verify Sigstore attestation, verify sha256 yourself, then install). **`curl | bash` is the convenience path, not the production-recommended path.** No external security audit has been performed — see [SECURITY.md](SECURITY.md) before deploying.

Already familiar? The short version, macOS / Linux:

1. **Create an Odoo API key.** Top-right profile → My Profile → Account Security → New API Key. Name it `odoo-mcp`. Copy it — Odoo only shows it once. Any internal user can create their own; no admin rights needed. Don't use an admin user's key on production (the MCP refuses by default).

2. **Run the installer.**

   ```bash
   brew install gh && gh auth login
   curl -fsSL https://raw.githubusercontent.com/deltix-consulting/odoo-mcp/main/scripts/install.sh | bash
   ```

   It verifies the release attestation, asks for Odoo URL / database / login / API key, and stores credentials in your OS credential store.

3. **Restart Claude Desktop / Claude Code / Codex** so they load the MCP. Ask: *"use odoo_help to show what you can do with my Odoo"*.

Windows: PowerShell `iwr -useb https://raw.githubusercontent.com/deltix-consulting/odoo-mcp/main/scripts/install.ps1 | iex`.

For deltix-internal onboarding (admin preps a colleague), see [ONBOARDING.md](ONBOARDING.md).

### Other MCP clients

The installer registers Claude Desktop and Codex automatically. For Cursor, Windsurf, Continue, Zed, or any other MCP-compliant client, run:

```bash
odoo-mcp client-config --client cursor      # or windsurf / continue / zed / claude-code / generic-stdio
odoo-mcp client-config --detect             # print snippets for every client whose config dir is found locally
odoo-mcp client-config --list               # see the full supported list
```

The command resolves the absolute `odoo-mcp` path on your machine and prints a paste-ready snippet plus the file it goes in.

## Privacy posture in detail

- **No telemetry.** The only outbound HTTP is to your Odoo, plus GitHub during `odoo-mcp update`.
- **No phone-home.** deltix-consulting receives nothing — no URL, database name, queries, results, or audit log.
- **No source-code upload.** Custom modules are discovered at runtime via `ir.model` / `ir.model.fields`. Source stays in your repo.
- **Credentials never on disk.** Username and API key live in your OS credential store (macOS Keychain / Windows Credential Manager / libsecret), are scrubbed from errors, and are deleted from `os.environ` after auth.
- **Audit log stays local.** `~/.odoo-mcp/audit.jsonl` records metadata only — no field values, no queries, no results.
- **Shared responsibility.** See [SECURITY.md](SECURITY.md#user-responsibilities) for what the operator must still own.

See [SECURITY.md](SECURITY.md) for the full threat model.

## Tools

| Tool | Purpose |
|---|---|
| `odoo_help` | Capability overview, no Odoo round-trip |
| `odoo_list_instances` | Configured instances and their state |
| `odoo_describe_model` | Field metadata for one model |
| `odoo_lookup` | Fast `name ilike` lookup, returns id + display_name |
| `odoo_search_read` | Query records with explicit fields and a sandboxed domain |
| `odoo_search_count` | Count records matching a domain |
| `odoo_read_group` | Aggregate (sum/avg/count/min/max) with groupby |
| `odoo_read` | Read specific records by ID |
| `odoo_default_get` | Preview the defaults Odoo would auto-fill on a new record |
| `odoo_create` | Create a record (prod-gated) |
| `odoo_write` | Update records (prod-gated) |
| `odoo_archive_or_delete` | Archive or `unlink` records (prod-gated) |
| `odoo_enable_prod_writes` | Unlock prod writes for 15 minutes |
| `odoo_diagnose_access` | Report the user's read/write/create/unlink rights on a model |

No `execute_kw`. No workflow buttons. No `copy`, `name_search`, `fields_view_get`. `unlink` is reachable only through `odoo_archive_or_delete`.

## CLI

| Command | Purpose |
|---|---|
| `odoo-mcp onboarding` | Guided first run: setup wizard + doctor + scan, writes `~/.odoo-mcp/suggestions.toml` |
| `odoo-mcp setup` | First-time wizard (config, credentials, Claude Desktop + Codex registration) |
| `odoo-mcp setup --add` | Add another instance |
| `odoo-mcp setup --remove` | Remove an instance and its Keychain entries |
| `odoo-mcp setup --list` | List configured instances |
| `odoo-mcp setup --rotate-key NAME` | Rotate the API key for one instance (paste a new key) |
| `odoo-mcp renew-key NAME` | Generate + store a fresh API key via password (for Odoo Online's 1-day expiry policy) |
| `odoo-mcp setup --regenerate-launcher` | Rewrite `launch.sh` |
| `odoo-mcp uninstall` | Remove config, Keychain entries, launcher, Claude Desktop + Codex registration, and the `uv tool` install |
| `odoo-mcp doctor` | Pre-flight: config perms, audit log, TLS, auth, smoke call |
| `odoo-mcp status` | Live status: auth, unlock state, rate-limit budget |
| `odoo-mcp audit` | Audit log inspector |
| `odoo-mcp cache --info` / `--clear` | Persistent fields-cache stats / drop entries |
| `odoo-mcp scan-custom INSTANCE` | Discover custom models and likely-sensitive fields, suggest TOML overrides |
| `odoo-mcp client-config` | Print MCP client config snippets for Claude Desktop, Cursor, Windsurf, Continue, Zed, Codex, ... |
| `odoo-mcp update` | `git pull` + `uv sync` against the install directory |

## Compatibility

> **No external security audit has been performed.** Treat the
> compatibility table below as community-tested. For production
> deployments, review [SECURITY.md](SECURITY.md) and the
> [safe production setup checklist](SECURITY.md#safe-production-setup-checklist).

| Surface | Versions | Status |
|---|---|---|
| Odoo Community | 17.0, 18.0 | tested |
| Odoo Enterprise | 17.0, 18.0 | tested |
| Odoo Online (SaaS) | current | tested — note: non-admin API keys expire in 1 day; use `odoo-mcp renew-key` |
| Odoo Community / Enterprise | 16.0 | should work, lightly tested |
| Odoo 19+ / JSON-2 | — | planned, see [ROADMAP.md](ROADMAP.md) |
| Python | 3.11, 3.12, 3.13 | tested |
| Operating system | macOS 12+, Windows 10/11, Linux (libsecret) | tested |
| MCP clients | Claude Desktop, Claude Code, OpenAI Codex CLI, Cursor, Windsurf, Continue.dev, Zed | snippets shipped — see `odoo-mcp client-config --list` |

If a combination is missing or broken, file an [Odoo compatibility bug](https://github.com/deltix-consulting/odoo-mcp/issues/new?template=odoo-compat-bug.yml).

## Example output

A few sample interactions so you know what to expect before installing.

### `odoo-mcp doctor`

```text
  ✓ Load config — from /Users/you/.odoo-mcp/config.toml
  ✓ Audit log writable — /Users/you/.odoo-mcp/audit.jsonl
  ✓ [prod] credentials — user you@yourcompany.com
  ✓ [prod] authenticate — uid=42
  ✓ [prod] fields_get(res.partner) — 137 fields

OK
```

### `odoo_help` (terse mode)

```json
{
  "ok": true,
  "version": "0.16.2",
  "summary": "Security-gated Odoo over XML-RPC: per-instance allowlists, domain sandbox, field redaction, prod-write guard with dry-run + confirmation tokens. Call odoo_help(verbose=true) for the full cookbook.",
  "tools": [
    {"name": "odoo_search_read", "purpose": "Search + read with explicit fields list."},
    {"name": "odoo_diagnose_access", "purpose": "Read/write/create/unlink rights on a model."},
    ...
  ],
  "instances": [
    {"name": "prod", "url": "https://yourco.odoo.com", "production": true,
     "writes_unlocked": false, "allowlist_mode": "strict"}
  ]
}
```

### Dry-run write on production

Call `odoo_write` for the first time after unlocking prod:

```json
{
  "ok": true,
  "preview": true,
  "instance": "prod",
  "model": "crm.lead",
  "id_count": 1,
  "would_update_fields": ["stage_id"],
  "confirmation_token": "conf_XXXXXXXXXXXXXXXXXXXX",
  "note": "This was a dry run. To commit, call odoo_write again with dry_run=false and confirmation_token set to the token above."
}
```

Same call with `dry_run: false, confirmation_token: "conf_..."`:

```json
{
  "ok": true,
  "instance": "prod",
  "model": "crm.lead",
  "ids": [42],
  "committed": true,
  "commits_remaining": 9
}
```

### `odoo-mcp audit --stats`

```text
TOOL                       CALLS  OK   ERR  P50ms  P95ms  MAXms
odoo_search_read           312    310    2    87    254    611
odoo_describe_model         48     48    0    34     91    187
odoo_read_group             18     18    0   102    188    220
odoo_diagnose_access         4      4    0    71    108    113
odoo_enable_prod_writes      2      2    0     8     12     14
odoo_write                   2      2    0    96    102    102
```

## Production write workflow

Writes against any instance with `production = true` go through three independent, server-enforced gates.

1. **Unlock.** `odoo_enable_prod_writes(instance="prod")` flips a 15-minute activity-based flag. Each write refreshes the expiry; 15 minutes of silence relocks.
2. **Dry-run preview.** First write defaults to `dry_run=true`. The server validates the payload, returns a preview, and issues a single-use `confirmation_token` bound to `(instance, operation, model)`.
3. **Commit.** Call again with `dry_run=false` and the token. Single-use — a second commit needs a second dry run.

```text
# 1. Unlock
odoo_enable_prod_writes(instance="prod")

# 2. Dry run — returns { preview: true, confirmation_token: "abc123", ... }
odoo_write(instance="prod", model="crm.lead", ids=[42], values={"stage_id": 3})

# 3. Commit
odoo_write(instance="prod", model="crm.lead", ids=[42],
           values={"stage_id": 3},
           dry_run=false, confirmation_token="abc123")
```

A burst limit (`max_commits_per_unlock`, default 10) caps real commits per unlock; dry-runs do not count. Production instances also refuse to authenticate as Odoo admin (`uid=1` or `base.group_system`) — create a dedicated non-admin user instead. Override with `refuse_admin_on_production = false` only if you know what you're doing.

On non-production instances, writes commit directly.

## Updating

Run `odoo-mcp update`. This runs `git pull` in the install directory followed by `uv sync`. It does not touch `~/.odoo-mcp/config.toml` or Keychain entries. Existing installs auto-migrate Claude Desktop to the direct `odoo-mcp launch` registration and add the same registration to Codex when Codex is installed.

Releases are signed via Sigstore using GitHub Actions Build Provenance Attestations. The installer verifies attestations before extracting; verify a downloaded tarball manually with `gh attestation verify`. See "Verifying releases" in [SECURITY.md](SECURITY.md).

## Configuration

Configuration lives at `~/.odoo-mcp/config.toml` (chmod 600 — the server refuses to start otherwise). The onboarding wizard generates it; advanced operators can edit it by hand. The schema is defined in `src/odoo_mcp/config.py`. Key knobs: `timeout_seconds`, `max_records_default`, `max_records_hard_cap`, `allowed_models` (open `["*"]` or strict list), and per-instance `[instances.NAME]` blocks with `url`, `database`, `credentials_env_prefix`, `production`.

## Reference

- **Allowed models.** Default is open mode (`allowed_models = ["*"]`): every model reachable except a hardcoded denylist (auth/users/groups, ACLs, stored executable content, system config, scheduler, raw attachments, model metadata). Switch to strict mode by listing models explicitly. The denylist always applies on top. See `src/odoo_mcp/config.py` for the denylist.
- **Sensitive fields.** Two tiers: regex-matched fields (passwords, tokens, api_keys, salary/payroll/bonus, secrets, credentials) are always redacted and unwritable. Per-model default-hidden fields (e.g. `res.partner.vat`, `hr.employee.ssnid`) require per-call `allow_sensitive_fields=[...]`. Extend with `custom_sensitive_field_patterns` per instance.
- **Audit log.** One JSONL line per call to `~/.odoo-mcp/audit.jsonl`. Daily rotation, 30-day retention. No values, no domains, no record content. Server fails closed if the log becomes unwritable. Inspect with `odoo-mcp audit`.
- **Caching.** L1 in-memory + L2 persistent SQLite (`~/.odoo-mcp/fields-cache.db`, 24h TTL) for `fields_get` only — never record values. Disable L2 with `fields_cache_path = ""`. Drop stale entries with `odoo-mcp cache --clear`.
- **Debug logging.** Export `ODOO_MCP_LOG_LEVEL=DEBUG|INFO|WARNING|ERROR` to stream to stderr. Credentials are scrubbed.
- **Runtime scoping env vars** (all optional, all read at call time):

  | Var | Effect |
  |---|---|
  | `ODOO_MCP_READ_ONLY=1` | Refuses every write-path tool (`odoo_create`, `odoo_write`, `odoo_archive_or_delete`, `odoo_enable_prod_writes`) regardless of per-instance `production` flag. Reads unaffected. Truthy values: `1` / `true` / `yes` / `on`. |
  | `ODOO_MCP_DISABLE_TOOLS=odoo_write,odoo_create` | Filters tools out of the MCP `tools/list` advertisement. A well-behaved client never sees them. Comma-separated; whitespace tolerated; unknown names logged and ignored. |
  | `ODOO_MCP_TOOL_LATENCY_BUDGET_MS=2000` | Pure observability. Dispatcher logs a `WARNING` tagged `slow_tool_call` whenever a successful call exceeds the budget. Set to a non-positive integer or unset to disable. |

  All three surface in `odoo-mcp doctor` so they're visible in pre-flight.
- **Industry templates.** `templates/` ships per-industry starting points for advanced operators. Most users should run `odoo-mcp onboarding`, which produces a per-instance `suggestions.toml` from the live schema. See [INDUSTRY_AUDIT.md](INDUSTRY_AUDIT.md).

## Development

```bash
git clone https://github.com/deltix-consulting/odoo-mcp.git
cd odoo-mcp
uv sync --extra dev
uv run pytest -q
```

Integration tests need `ODOO_MCP_TEST_*` environment variables and `pytest -m integration`. CI runs ruff, ruff format, mypy strict, and pytest on every push.

## Security reporting

Email `hello@deltix.pro` (subject: `[odoo-mcp security]`) and expect a response within five business days. See [SECURITY.md](SECURITY.md).

## License

MIT — see [LICENSE](LICENSE).
