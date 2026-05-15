# Getting started with odoo-mcp

This is a step-by-step guide for someone who has never run `odoo-mcp`
before. It covers what to prepare, how to create an Odoo API key, what
permissions you need, how to install, and what to do when things go
wrong.

For the broader project context see the [README](README.md). For the
security model see [SECURITY.md](SECURITY.md). For deltix-internal
consultant onboarding see [ONBOARDING.md](ONBOARDING.md).

---

## 1. Before you start

Have these four things ready. The installer asks for them.

| | |
|---|---|
| **The URL of your Odoo** | e.g. `https://yourcompany.odoo.com` (SaaS) or `https://erp.yourcompany.com` (self-hosted). The full URL, with `https://`, no trailing path. |
| **The database name** | The Odoo database your user lives in. Find it: log in to Odoo, look at the URL — it appears after `/web?db=...` if you have multiple databases, or visit `<url>/web/database/manager`. SaaS users typically have one database named like the URL prefix. |
| **Your Odoo login** | The email or username you sign in to Odoo with. |
| **An Odoo API key** | Generated in your Odoo profile. See section 3. Do *not* use your password — the MCP uses XML-RPC with an API key. |

You also need:

- **macOS 12+, Windows 10+, or Linux** (Linux needs `libsecret` for the credential store; pre-installed on most distros, otherwise `sudo apt install libsecret-1-0 gnome-keyring`).
- **A GitHub account** with `gh` CLI authenticated — the installer uses it to fetch + verify the release. The installer will bootstrap Homebrew + `gh` on macOS if missing.
- **An MCP client.** Claude Desktop, Claude Code, OpenAI Codex CLI, Cursor, Windsurf, Continue.dev, or Zed all work. The installer registers Claude Desktop and Codex automatically; for the others, `odoo-mcp client-config` prints paste-ready snippets after install.

---

## 2. Decide which Odoo user to authenticate as

This is the single most important decision. **The MCP will act in Odoo as whichever user owns the API key.** All record rules, access rights, and audit trails attach to that user.

**Strongly recommended:** create a dedicated Odoo user, not your own. Reasons:

- You want the MCP's writes (and reads of sensitive fields) to be attributable to "the MCP integration" in the Odoo log, not to you personally.
- You want to control what the MCP can see and do via Odoo's normal group / record-rule system, independently of what you personally can do.
- If the API key leaks, you revoke the dedicated user, not your own login.

**Do not** use an admin user (`Settings → Users & Companies → Groups → Administration: Settings`) on production. Admin keys bypass most Odoo record rules; the MCP refuses them by default on production instances and will print a clear error. You can opt out via config, but you really shouldn't.

**Minimum groups for typical MCP use:**

| You want to... | Give the user these groups |
|---|---|
| Read contacts | `Contacts: User` |
| Read leads / opportunities | `Sales: User: Own Documents Only` (or `All Documents`) |
| Read sales orders + invoices | `Sales: User` + `Accounting: Billing` (read-only) |
| Read inventory | `Inventory: User` |
| Read HR data (cautious) | `Human Resources: Officer` — but lots of PII; review the redaction defaults in `src/odoo_mcp/security/fields.py` first |
| Read project tasks + timesheets | `Project: User` + `Timesheets: User` |
| **Write** to any of the above | Add the matching `Manager` / write group only if you really want the MCP to be able to make changes |

Start narrow. You can always grant more later by editing the user in Odoo.

---

## 3. Get an Odoo API key

API keys are how non-interactive integrations authenticate to Odoo. They are individual and revocable.

**You have two ways to get one.** The setup wizard (`odoo-mcp setup`) asks which you prefer:

> **Option 2 — let the wizard generate it (recommended).** When the wizard
> asks how to authenticate, pick option 2. You type your normal Odoo
> password once; the wizard authenticates, generates the API key for you,
> stores it in the OS credential store, and discards the password
> immediately. You never touch the Odoo UI. **This does not work if your
> account has 2FA enabled** — 2FA blocks password authentication over the
> API, so 2FA users must use option 1 below.

> **Option 1 — create it yourself in Odoo (manual).** Follow the
> step-by-step below, then paste the key when the wizard asks. Required
> for 2FA accounts; also fine if you simply prefer to create it yourself.

The rest of this section covers Option 1. If you're using Option 2 you can skip ahead to [section 4](#4-install-odoo-mcp).

### Step-by-step in Odoo 16 / 17 / 18 (Option 1, manual)

1. **Log in to Odoo** as the user you want the MCP to act as.
2. **Top-right corner**, click your profile photo or initials.
3. Pick **"My Profile"** (or *"Preferences"* on older versions).
4. In the dialog, open the **"Account Security"** tab.
5. Click **"New API Key"**.
6. Odoo asks you to **re-authenticate** with your password. This is normal — API keys are sensitive.
7. **Name the key** something memorable: `odoo-mcp` is fine. If multiple MCPs share this user, include a suffix: `odoo-mcp-laptop`, `odoo-mcp-ci`.
8. Click **Generate Key**.
9. **Copy the key now.** Odoo shows it exactly once. If you close the dialog without copying, you have to delete the key and create a new one. Paste it temporarily into a password manager or a sticky note you will delete in five minutes — the installer puts it into the OS credential store, after which you should never need it again.

### "I don't see an API Keys tab"

A few causes:

- **The user has no password set** (e.g. it was created via OAuth-only signup). Set a password first via *Settings → Users → Reset Password*, then create the key.
- **You are on an old Odoo (≤ 13).** The XML-RPC endpoint in old Odoo accepts password authentication directly; the MCP supports this, but you lose the per-key revocation property. Strongly recommend upgrading Odoo before using the MCP in production.
- **The user is a Portal or Public user.** Only internal users can create API keys. Promote the user to *Internal User* or create a separate internal user.

### Permissions needed to create an API key

**None special.** Any internal Odoo user can create their own API keys for themselves. You do *not* need admin rights, you do *not* need to be in the Settings group, and you do *not* need to ask an admin. The key inherits whatever permissions the user already has.

If the *"New API Key"* button is greyed out or missing, that's an Odoo configuration issue on your tenant (developer mode disabled at a database level, an installed module overriding the security view, etc.), not a permission issue. Ask your Odoo administrator.

---

## 4. Install odoo-mcp

One command per platform.

### macOS / Linux

```bash
# Install the gh CLI if you don't have it (macOS):
brew install gh
gh auth login

# Run the installer:
curl -fsSL https://raw.githubusercontent.com/deltix-consulting/odoo-mcp/main/scripts/install.sh | bash
```

The script:

1. Verifies the latest release's [Sigstore Build Provenance Attestation](https://docs.github.com/en/actions/security-for-github-actions/using-artifact-attestations).
2. Installs the `odoo-mcp` CLI via `uv tool install`.
3. Prompts for the four things from section 1 (URL, database, login, API key).
4. Stores the credentials in your OS credential store (Keychain on macOS, libsecret on Linux).
5. Writes `~/.odoo-mcp/config.toml` with the right TOML shape.
6. Registers the MCP with Claude Desktop and Codex if those are installed.
7. Runs `odoo-mcp doctor` to verify everything works.

### Windows 10/11

PowerShell, run as a regular user (not as admin):

```powershell
iwr -useb https://raw.githubusercontent.com/deltix-consulting/odoo-mcp/main/scripts/install.ps1 | iex
```

Same flow as macOS but using Windows Credential Manager.

### Manual install (when the script can't run)

```bash
# Install uv (https://docs.astral.sh/uv/getting-started/installation/)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone and install
git clone https://github.com/deltix-consulting/odoo-mcp.git
cd odoo-mcp
uv tool install --editable .

# Run setup
odoo-mcp setup
```

---

## 5. Verify it works

```bash
odoo-mcp doctor
```

You should see:

```
  ✓ Load config — from /Users/you/.odoo-mcp/config.toml
  ✓ Audit log writable — /Users/you/.odoo-mcp/audit.jsonl
  ✓ [prod] credentials — user you@yourcompany.com
  ✓ [prod] authenticate — uid=42
  ✓ [prod] fields_get(res.partner) — 137 fields

OK
```

If you see warnings about admin credentials or missing API key rotation, read them — they're informational, not failures.

Then **restart Claude Desktop / Claude Code / Codex** so they load the MCP. In your MCP client, ask:

> *"Use odoo_help to show me what you can do with my Odoo"*

You should get back a list of 13 tools and a summary of how the security gates work.

---

## 6. First real test (read-only, dev-friendly)

Don't start with a write on production. Start with these:

| Ask the MCP | What it does |
|---|---|
| *"How many customer records do we have?"* | Calls `odoo_search_count` on `res.partner` |
| *"Show me the last 10 leads created"* | Calls `odoo_search_read` on `crm.lead`, order by `create_date desc` |
| *"Group sales orders by stage and total value"* | Calls `odoo_read_group` on `sale.order` |
| *"Describe the fields on hr.employee"* | Calls `odoo_describe_model`. Default-hidden fields like `ssnid` are marked `_sensitive: true` |

For pre-canned consultant workflows, your MCP client may show slash-commands like `/odoo_month_end_check`, `/odoo_top_revenue_customers`, `/odoo_find_duplicate_partners`. See `odoo_help(verbose=true)` for the full list.

---

## 7. Common gotchas

| Symptom | Cause | Fix |
|---|---|---|
| `Authentication failed` | Wrong database name (case-sensitive) | Check `/web/database/manager` or your URL |
| `Authentication failed` even with right db | Wrong login | Use the email or login string exactly as it appears in *Settings → Users* |
| `Authentication failed` only on prod | You enabled 2FA after creating the key | Create a new key after enabling 2FA — old ones become invalid |
| Doctor refuses admin key on prod | You used an admin user's key | Create a non-admin user (section 2) and rotate with `odoo-mcp setup --rotate-key prod`. Or opt out with `odoo-mcp setup --acknowledge-admin prod` if you understand the trade-off |
| `Refusing unverified tarball` | `gh attestation verify` failed | Pass `--skip-verification` to the installer if you trust the release tarball you just downloaded |
| Claude Desktop doesn't see the MCP | You didn't restart it after install | Quit fully (not just close window) and reopen |
| `odoo-mcp client-config --detect` finds nothing | Your IDE config dir is somewhere unusual | Pass `--client NAME` explicitly. See `--list` for supported clients |
| Tool call returns `model_not_allowed` | Model is on the hardcoded denylist (`res.users`, `ir.attachment`, ...) | This is by design — see [SECURITY.md](SECURITY.md). Cannot be overridden |
| Field comes back as `null` or `<redacted>` | Sensitive field default-hidden | Pass `allow_sensitive_fields=["NAME"]` per call to opt in |

---

## 8. Rotation and revocation

API keys do not expire on their own. The MCP nags you every 90 days via `odoo-mcp doctor`.

To rotate:

```bash
odoo-mcp setup --rotate-key prod
```

This walks you through creating a new key in Odoo, then updates the credential store. Old key stays valid in Odoo until you delete it manually (do this from the same *Account Security* tab where you created it).

To revoke (e.g. laptop lost): delete the key in Odoo's Account Security tab. The MCP will fail loudly the next time anyone tries to use it.

---

## 9. Where to get help

- Doctor output usually contains the exact next step. Read it before asking.
- `odoo-mcp help` lists every CLI subcommand.
- `odoo_help` (the tool, not the CLI) lists every MCP tool inside your AI client.
- Bug or security issue → [GitHub issues](https://github.com/deltix-consulting/odoo-mcp/issues) or `hello@deltix.pro` (subject `[odoo-mcp security]`) for sensitive reports.
- Project context: [README](README.md). Threat model: [SECURITY.md](SECURITY.md).
