# Onboarding a colleague to the Odoo MCP

This guide covers the steps to get a deltix consultant — or anyone else with
their own Odoo user — connected to Claude Cowork and Codex via the MCP. The whole
process takes about ten minutes per person if everything is in place.

The MCP is **per-user, local-first**: each colleague runs their own copy on
their own laptop, authenticates as themselves against Odoo, and inherits
their own Odoo permissions. There is no shared service account.

---

## Part 1 — Admin prep (one-time per colleague)

You do this in Odoo as an admin before sending the onboarding instructions.

### 1. Create or identify the colleague's Odoo user

Use a real Odoo user — typically the one they already log in with. If they do
not have one yet, create it via *Settings → Users & Companies → Users*.

### 2. Assign only the groups they actually need

Open the user record and review the *Access Rights* tab. Recommended starting
points by role:

| Role | Suggested Odoo groups |
| --- | --- |
| Sales consultant | `Sales / User: Own Documents Only` |
| Senior sales | `Sales / User: All Documents` |
| Project consultant | `Project / User`, `Sales / User: Own Documents Only` |
| Bookkeeper | `Invoicing / Billing` or `Accounting / Accountant` |
| Office manager | `Project / User`, `Knowledge / User`, `Calendar / User` |
| Junior / intern | only the modules they actively work in |

Do **not** assign these unless there is a concrete need:

- `Administration / Settings` — this is the `base.group_system` flag. The MCP
  refuses to authenticate against a production instance with a system-admin
  user (see "Why no admin keys" below).
- `Accounting / Adviser` — sees every journal in every company.
- `Human Resources / Officer` — sees all employee files including salaries.

### 3. Review record rules where useful

Most "Own Documents Only" groups already attach a record rule that scopes
the user to their own records. If the colleague needs access only to a
subset (e.g. only their team's leads), set a custom record rule under
*Technical → Security → Record Rules*.

Reminder: the MCP applies its own allowlist + redaction layers on top, but
record-level scoping is enforced by Odoo. If Odoo's rules are loose, the
MCP can't tighten them.

### 4. Map the klant's custom-field surface (`scan-custom`)

After the colleague's MCP is configured for the klant instance, run:

```bash
odoo-mcp scan-custom <instance>             # human-readable report
odoo-mcp scan-custom <instance> --toml      # paste-ready config snippet
```

The scan diffs the klant's Odoo schema against the embedded Odoo
Community 18.0 reference and surfaces:

- **Custom models** (Studio + bespoke modules + OCA additions).
- **Custom fields on standard models** with a sensitivity verdict
  (`BLOCKED` / `GATED` / `LIKELY_SENSITIVE` / `LIKELY_FINANCIAL` /
  `BINARY_AUTO_STRIPPED` / `UNCERTAIN`). Dutch / Flemish keywords
  (`loon`, `geboorte`, `vertrouwelijk`, `rijksregister`, etc.) are
  recognised.

Take the `--toml` output, review each entry, and paste the resulting
`custom_sensitive_field_patterns` + `sensitive_fields` blocks into the
klant's instance section of `~/.odoo-mcp/config.toml`. UNCERTAIN
findings warrant a manual look — they fall through to the safe default
(no opt-in needed for the MCP client to read them) but might still be klant-
sensitive in context.

### 5. Confirm GitHub access

The MCP installer pulls the latest release from
`deltix-consulting/odoo-mcp` (private repo). The colleague must be able to
read it. Either:

- Add them as a collaborator on `deltix-consulting/odoo-mcp` in GitHub, or
- Use a deltix-organization-wide read role.

Without this, `gh release download` in the installer will fail.

---

## Part 2 — Colleague's onboarding (send them this)

Forward the section below — everything from "Prerequisites" down — to the
colleague. They run it on their own machine.

---

### Prerequisites

- macOS (Apple Silicon or Intel).
- Claude Cowork and/or Codex installed.
- Homebrew available (`brew --version` should work). If not, install from
  https://brew.sh.

### Step 1 — Authenticate the GitHub CLI

The MCP installer pulls a private release from the `deltix-consulting`
GitHub organization, so it needs your `gh` to be logged in once.

    brew install gh
    gh auth login

Choose **GitHub.com** → **HTTPS** → authenticate via the browser. After it
finishes, run:

    gh auth status

You should see `Logged in to github.com as <your-handle>`. If you cannot
read `deltix-consulting/odoo-mcp`, contact your deltix admin.

### Step 2 — Create your Odoo API key

Generate the key as **yourself**, not as a shared "service" user. The MCP
will use this key for every request, and Odoo will see those requests as
coming from you — your record rules and field permissions apply directly.

1. Log in to Odoo at the URL your admin gave you (e.g.
   `https://deltix.odoo.com`) as your normal user.
2. Click your profile picture (top right) → **My Profile**.
3. Open the **Account Security** tab.
4. Click **New API Key**.
5. Name it something recognisable, e.g. `odoo-mcp-firstname`. Copy the key
   immediately — Odoo only shows it once.

Treat this string the same way you treat your password.

### Step 3 — Run the installer

In Terminal:

    curl -fsSL https://raw.githubusercontent.com/deltix-consulting/odoo-mcp/main/scripts/install.sh | bash

The installer will:

1. Verify you are on macOS and have `gh` authenticated.
2. Install `uv` (the Python package manager) if it is missing.
3. Download the latest signed release tarball into `~/odoo-mcp`.
4. Install dependencies and start the interactive setup wizard.

The wizard asks for:

- **Instance name** — a short label for this connection. Press Enter to
  accept `main`, or use something like `deltix-prod`.
- **Odoo URL** — exactly what your admin sent you, e.g.
  `https://deltix.odoo.com`.
- **Database name** — usually `deltix` (your admin will confirm).
- **Production?** — answer `y`.
- **Username** — your Odoo login email.
- **API key** — paste the key you copied in Step 2. The terminal does not
  echo it; that is normal.

The wizard then:

- Stores your username and API key in the macOS Keychain (no plaintext
  files anywhere).
- Generates `~/.odoo-mcp/config.toml` (chmod 600).
- Registers the MCP in Claude Cowork's config and, when Codex is installed,
  in Codex's config.
- Runs a health check (`doctor`) end-to-end.

If `doctor` ends with `OK` you are done with the install. If you see
`FAILED`, capture the output and contact your deltix admin.

### Step 4 — Restart Claude Cowork and Codex

The MCP is loaded by Claude Cowork and Codex at start-up. Quit the app fully
(**Cmd+Q**, not just close the window) and re-open it. Tools named
`odoo_*` should then be available.

### Step 5 — Verify

In a fresh Cowork or Codex session, ask:

> Use `odoo_help` to show what this MCP can do.

You should see a structured response listing tools, common patterns, and
your instances. Then try a real query:

> Show me my five most recent leads.

If the response looks reasonable, you are connected and scoped correctly.

To verify your scoping is enforced rather than wide-open, deliberately ask
for something **outside** your role. For example, if you do not have HR
access:

> Show me the salaries of all employees.

You should see either an Odoo permission error or an empty list — never the
actual data. That is the per-user scoping working as intended.

---

## Part 3 — Day-to-day commands

Run these from Terminal whenever you need them:

| Command | Purpose |
| --- | --- |
| `odoo-mcp doctor` | Full health check: config, audit log, credentials, auth, smoke fields_get |
| `odoo-mcp status` | Live snapshot: per-instance auth state, rate-limit budget, write-lock state, last 5 audit entries |
| `odoo-mcp audit --tail 20` | Most recent 20 audit log entries |
| `odoo-mcp audit --errors` | Only failures from the last 24h |
| `odoo-mcp config show` | What the MCP is configured to do (no secrets) |
| `odoo-mcp cache --info` | Persistent fields_get cache status |
| `odoo-mcp update` | Pull and install the latest release (verifies attestation) |
| `odoo-mcp setup --rotate-key NAME` | Rotate the API key for one instance |
| `odoo-mcp setup --remove` | Tear down an instance (Keychain + config) |
| `odoo-mcp client-config --detect` | Print MCP config snippets for every IDE / chat client found locally |
| `odoo-mcp client-config --client cursor` | Snippet for one specific client (cursor / windsurf / continue / zed / codex / claude-desktop / claude-code / generic-stdio) |
| `odoo-mcp audit --stats` | Per-tool call counts + p50/p95/max latency |
| `odoo-mcp doctor --json` / `audit --json` / `cache --info --json` / `status --json` | Machine-readable output for CI / scripts |

### Optional runtime scoping

Three env vars tighten the MCP without touching code or config. Set them in
the shell that launches the MCP (typically your IDE / Claude Desktop session,
or `~/.zshrc` / `~/.bashrc`).

| Env var | What it does |
| --- | --- |
| `ODOO_MCP_READ_ONLY=1` | Refuses every write tool. Reads still work. Useful for demos, training, external consultants. |
| `ODOO_MCP_DISABLE_TOOLS=odoo_create,odoo_write,odoo_archive_or_delete` | Hides tools from the MCP client entirely (they don't show up in `tools/list`). |
| `ODOO_MCP_TOOL_LATENCY_BUDGET_MS=2000` | Logs a warning whenever a successful tool call exceeds N ms — helps spot runaway loops or slow models. |

`odoo-mcp doctor` surfaces these so a colleague who flipped one for a demo
can see the active gates in pre-flight.

### Production write workflow

Writes to a production instance are blocked by default. To make changes:

1. Tell Claude or Codex what you want.
2. The client calls `odoo_enable_prod_writes(instance="...")` to unlock for
   15 minutes.
3. The client attempts the write; you receive a **dry-run preview** with a
   one-time confirmation token.
4. Review the preview. If you agree, tell the client to commit. It calls
   the write again with `dry_run=false` and the token.
5. Within one unlock window you have a budget of 10 commits by default
   (configurable via `max_commits_per_unlock`). Dry-runs do not count.

This three-step flow exists so a hallucinating loop cannot quietly mutate
production. It looks heavy at first but settles into rhythm.

---

## Part 4 — Updating

When deltix ships a new version:

    odoo-mcp update

This:

1. Checks GitHub for a newer release.
2. Downloads the release tarball and verifies the build-provenance
   attestation (skipped with a warning if `gh` is unavailable; refused
   on a hard verification failure).
3. Pulls the new code via `git pull --ff-only` after you confirm.
4. Re-runs `uv sync` and the test suite.
5. Re-runs `doctor` for sanity.

`odoo-mcp update --check` only inspects whether something newer exists,
without applying anything.

To bypass attestation verification deliberately (e.g. on a machine without
`gh`):

    odoo-mcp update --skip-verification

Use sparingly and only when you trust the source.

---

## Part 5 — Offboarding

When a colleague leaves the team or rotates roles:

1. **Revoke the API key in Odoo** — *My Profile → Account Security →
   delete the `odoo-mcp-…` key*. This is the single source of truth
   for "this person can no longer access via the MCP". Do this first.
2. On their machine, run a single command to clean everything up:

       odoo-mcp uninstall

   This removes Keychain entries for every configured instance, the
   `odoo-mcp` entry in Claude Desktop / Cowork and Codex, the local config and
   launcher (`~/.odoo-mcp/`), the persistent fields cache, all audit
   logs, and the `uv tool` installation of `odoo-mcp` itself. The
   project checkout at `~/odoo-mcp` is intentionally left alone — the
   command prints the path and the colleague can `rm -rf` it manually
   once they have confirmed there is no uncommitted local work in it.

   Without step 2 the MCP just stops working when the API key is
   revoked — fail-closed — but their laptop will keep stale config
   and credentials sitting around. Step 2 is the proper hygiene.

If a key is suspected leaked, revoke immediately and notify your deltix
admin. The leaked key cannot be used to write to production without going
through the unlock + dry-run + token flow, but it can be used to read
within the user's Odoo permissions until revoked.

---

## Part 6 — Why no admin keys

Per the v0.5.0 default, the MCP **refuses** to authenticate against a
production instance when the API key belongs to:

- The Odoo superuser (`uid=1`, OdooBot), or
- Any user with the `base.group_system` group (the "Settings" admin
  flag).

Reason: admin-level credentials bypass most of Odoo's record rules and
field-level group restrictions. The MCP relies on those rules to scope
each user to what they actually need. If you authenticate as an admin,
you effectively turn the per-user scoping off — and most of the safety
case for using the MCP at all goes with it.

If you genuinely need admin credentials (integration test rigs, very
small deltix-internal staging Odoos, etc.) you can opt out per instance
by adding to that instance's TOML block:

    refuse_admin_on_production = false

The MCP will then start, log a clearly visible warning, and run with
ACL scoping disabled. The other safety layers (model denylist, prod-
guard, redaction, burst limits) still apply.

---

## Part 7 — Troubleshooting

**Claude or Codex says "no Odoo tools available" after install.**

Cmd+Q the app and re-open. Tools load only at process start. If they still
do not appear after a full restart, check the relevant config:

Claude:
`~/Library/Application Support/Claude/claude_desktop_config.json` —
the wizard adds an `odoo-mcp` entry under `mcpServers`.

Codex:
`~/.codex/config.toml` — the wizard adds an `odoo-mcp` entry under
`[mcp_servers.odoo-mcp]` when Codex is detected.

**doctor reports `! admin check`.**

Your API key is for an admin user. Ask the deltix admin to give you a
non-admin Odoo user instead, or — if you really mean it — set
`refuse_admin_on_production = false` in your instance config (see Part 6).

**doctor reports authentication failure.**

Most likely cause: the API key is wrong or revoked. Run
`odoo-mcp setup --rotate-key <instance>` to enter a fresh key.

**Tool calls fail with `model_not_allowed` for a model you expect.**

Some Odoo models are on the hard-coded denylist regardless of your
permissions: `res.users`, `ir.config_parameter`, `ir.actions.server`,
`mail.template`, `ir.attachment`, and a few more (run
`odoo-mcp config show` to see the count). These are blocked for safety
reasons and cannot be opened via config.

**Tool calls fail with `field_policy` on a custom-module field.**

The field name matched a built-in or per-instance redaction pattern
(common offenders: anything containing `salary`, `compensation`,
`bonus`, `payroll`). For the always-redacted built-ins this is a hard
block. For default-hidden fields (VAT, IBAN, employee birthday, etc.)
you can opt in per call by passing `allow_sensitive_fields=["fieldname"]`
to the tool — Claude or Codex will surface this in the tool arguments so you can
review before approving.

**`odoo-mcp update` warns about no attestation.**

The deltix-consulting GitHub org is currently on a free plan, which
does not allow signed attestations on private repos. Releases still
publish wheel + sdist, just unsigned. The verification step warns and
prompts; pick `y` to proceed if you trust the source. This will resolve
once the org plan is upgraded.

---

## Quick onboarding checklist

For the deltix admin to work through:

- [ ] Odoo user exists, with role-appropriate groups, **without**
      `Administration / Settings`.
- [ ] Record rules reviewed where applicable.
- [ ] Colleague added as collaborator on
      `deltix-consulting/odoo-mcp` (or has org-level read).
- [ ] Onboarding instructions (Part 2) sent to colleague.
- [ ] Colleague reports `doctor` returned `OK` and verification queries
      behave as expected.
- [ ] Colleague's API key name documented somewhere so it can be revoked
      on offboarding.
