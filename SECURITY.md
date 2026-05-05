# Security policy

This document describes the threat model `odoo-mcp` is designed
against, the defenses it implements, what it explicitly does not
protect against, and how to report vulnerabilities.

## Threat model

`odoo-mcp` is designed under the assumption that an LLM may invoke any
tool with any arguments, including arguments derived from
prompt-injected content (email bodies, PDFs, third-party tickets,
scraped web pages). It is not safe to trust that the model will "know
better" than to call `odoo_create` with attacker-chosen values. Every
guardrail is therefore enforced server-side, in the dispatcher
pipeline, not in the tool description.

The concrete threats we defend against:

1. **Accidental destructive writes on production.** The model
   hallucinates a `write` or `create` call, or is prompt-injected into
   issuing one, and mutates prod data.
2. **Credential exfiltration.** API keys or usernames leak via
   environment variables inherited by subprocesses, via error messages
   rendered back to the model, via the audit log, or via
   log-forwarded stack traces.
3. **Field-level PII leakage.** Financial identifiers (VAT, IBAN,
   bank account IDs), employee PII (SSN, private contact, family
   details), or credentials stored as fields (password hashes, API
   keys, OAuth tokens) get read and included in the model's context.
4. **Privilege escalation via domain traversal.** A domain filter
   like `[('create_uid.login', '=', 'admin')]` reaches across from an
   allowlisted model into `res.users`, which is not on the allowlist.
5. **Runaway resource consumption.** A query with no `limit`, a
   `read` on tens of thousands of IDs, or a tight call loop burns
   through Odoo worker capacity or the model's context budget.
6. **Unauthorized method execution.** The XML-RPC `execute_kw`
   endpoint is exposed through a thin shim that lets the caller
   invoke any method on any model (`action_confirm`, `button_cancel`,
   `unlink`, custom `action_do_scary_thing`).

**On usernames vs. API keys.** The error-redaction registry in
`odoo_mcp.errors` registers the API key for scrubbing but **not** the
username. This is deliberate. Usernames (typically email addresses) are
identifying-but-not-secret: a redacted error like
`"Access denied for <redacted>"` is significantly less useful for
diagnosis than `"Access denied for jan@deltix.pro"`, and the username is
already disclosed at every authentication step against the Odoo server.
We therefore treat usernames as PII rather than as credentials — they may
appear in error messages and operator-facing logs, but they are never
written to the audit log (which records only metadata and an instance
name) and never returned in tool responses.

## Defense layers

For each threat above:

**1. Destructive writes on prod.** Four gates, each independently
enforced:

- Instances marked `production = true` start read-only. Any `create`
  or `write` call raises `prod_write_locked` until
  `odoo_enable_prod_writes` is invoked.
- The unlock is 15 minutes of activity. Fifteen minutes of silence
  auto-relocks.
- Even when unlocked, writes default to `dry_run=true` on prod. The
  dry-run path validates the payload, returns a preview, and issues a
  single-use `confirmation_token` bound to
  `(instance, operation, model)`.
- A real commit requires `dry_run=false` plus the matching
  `confirmation_token`. Tokens are consumed atomically; a second
  commit needs a second dry run.

**2. Credential exfiltration.**

- Credentials are stored in the macOS Keychain, not on disk. The
  config file contains only a prefix name; the real values are pulled
  at launch by `launch.sh` calling `odoo-mcp launch-env`.
- After the server constructs its `Credentials` object, it deletes
  the originating variables from `os.environ`, so `subprocess.Popen`
  children do not inherit them.
- `OdooMcpError` and subclasses scrub registered secret values from
  their `user_message`, including chained causes.
- `Credentials.__repr__` returns a fixed redaction placeholder, so
  accidental `print(creds)` or `logger.exception(...)` calls cannot
  leak the API key.
- The audit log records no field values, no domain operands, no
  arguments — just operation metadata.

**3. Field-level PII leakage.**

- Always-redacted fields are identified by regex on the field name
  (`password`, `*_password`, `password_crypt`, `new_password`,
  `api_key`, `*_api_key`, `token`, `*_token`, `access_token`,
  `refresh_token`, `*_secret`). These are dropped from every response
  and blocked in every write payload. Regex rather than a fixed list
  so a new module's `<module>_api_key` field is caught by default.
- Default-hidden fields (VAT, bank, SSN, employee PII) require
  per-call `allow_sensitive_fields=[...]` opt-in. The field list
  lives in `src/odoo_mcp/security/fields.py` and is kept explicit
  rather than heuristic.
- `odoo_describe_model` marks default-hidden fields with
  `_sensitive: true` so the model knows an opt-in is required,
  without ever returning the value.
- `odoo_read_group` rejects default-hidden fields in `groupby`
  without opt-in, because grouping by a field effectively reveals its
  distinct values.
- Binary fields are replaced with a `<binary:N bytes>` placeholder
  unless `include_binary=true`.

**4. Privilege escalation via domain traversal.** The domain sandbox
(`src/odoo_mcp/security/domain.py`) walks every leaf tuple and rejects
any field name containing a dot. It also validates operators against a
closed set and verifies field names against the target model's
`fields_get`, so unknown fields fail loudly rather than being silently
ignored. The sandbox runs before any XML-RPC call, so a rejected
domain never reaches Odoo.

**5. Runaway resource consumption.**

- Per-instance token-bucket rate limiter.
- Record limits clamped server-side:
  `max_records_default` when the caller omits `limit`,
  `max_records_hard_cap` as an absolute ceiling.
- `read` and `write` cap the `ids` list length at
  `max_records_hard_cap`.
- `read_group` caps `groupby` at four dimensions.
- Per-call XML-RPC timeout (`timeout_seconds`, default 30s).

**6. Unauthorized method execution.** There is no `execute_kw`
surface at the MCP boundary. The twelve operations in
`odoo_mcp.security.allowlist.Operation` are the only ones the client
knows how to call: `search_read`, `search_count`, `read`,
`read_group`, `lookup`, `create`, `write`, `archive`, `unlink`,
`fields_get`, `help`, `list_instances`. `unlink` is reachable only
via `odoo_archive_or_delete` with `mode='delete'`. No arbitrary
methods. No workflow buttons. The client is a closed API, not a
passthrough.

Model-level allowlist runs independently: even within the allowed
operations, the server rejects any call targeting a model outside the
per-instance `allowed_models` frozen set.

## What we do NOT defend against

Being explicit about this matters:

- **Compromised host.** If an attacker has code execution as your OS
  user, they can read the running server's memory (which contains the
  decoded API keys), tamper with `config.toml`, or modify the audit
  log. That is outside the MCP's trust boundary. Use OS-level
  isolation (a dedicated user, a container, FileVault) if you need
  that.
- **Malicious Odoo server.** The MCP trusts that its configured Odoo
  instance returns what it was asked for. A hostile Odoo server could
  return malformed records, embedded markdown, or prompt-injection
  payloads in field values. The redaction layer strips fields by
  name, but it does not inspect the semantic content of field values
  it returns. Treat data from untrusted Odoo servers with the same
  suspicion you would treat data from any third-party API.
- **Side-channel attacks.** Timing analysis of rate-limiter refills,
  cache-timing against the Keychain, or power-analysis against the
  host are not in scope.
- **Quantum anything.** We do not claim post-quantum guarantees. The
  transport security relies on whatever TLS your Odoo instance
  negotiates.
- **Supply-chain attacks on dependencies.** We pin direct dependencies
  in `pyproject.toml` but do not vendor or reproduce-build them.
- **The model itself being an adversary.** If the model actively
  cooperates with an attacker to exfiltrate data one field at a time
  within the allowed policy (search, opt-in, read), the MCP's job is
  to make that visible to the human reviewer, not to prevent it. Tool
  call approvals exist precisely for this case.

## Verifying releases

Every release artifact (`*.whl` and `*.tar.gz`) published on GitHub
Releases since v0.6.0 is signed via Sigstore using GitHub Actions Build
Provenance Attestations. The attestation is published to GitHub's
transparency log and can be independently verified. To check a
downloaded tarball:

```bash
gh attestation verify \
  --owner deltix-consulting \
  --signer-workflow ".github/workflows/release.yml" \
  odoo_mcp-0.6.0.tar.gz
```

A successful verification proves the artifact was built by our public
release workflow on a tag push — not produced by a fork, a tampered
checkout, or a compromised maintainer machine. `odoo-mcp update`
performs the same check automatically before applying an update; pass
`--skip-verification` only if you have a specific reason to bypass it.

## What we never see

The MCP runs entirely on your machine. Specifically:

- **No telemetry.** The MCP makes no outbound HTTP calls except to
  your Odoo and (during `odoo-mcp update`) to GitHub for releases.
- **No phone-home.** deltix-consulting (the publisher) does not
  receive your URL, database name, queries, results, audit log,
  or anything else.
- **No source-code upload.** If you have custom Odoo modules, the
  MCP discovers them by reading your live Odoo's `ir.model` /
  `ir.model.fields` at runtime. Your module source code stays in
  your repo; we never ask for it.
- **Credentials never on disk.** Your Odoo username and API key
  live in macOS Keychain (encrypted at rest, gated by your login).
  They are not written to `config.toml`, `audit.jsonl`, error
  messages, or anywhere else.
- **Audit log stays local.** Every tool call is logged to
  `~/.odoo-mcp/audit.jsonl` on your machine. It records metadata
  (timestamp, tool name, instance, count, duration) but never
  field values, never queries, never results.

## User responsibilities

This MCP enforces several defence layers (allowlist, denylist, redaction,
prod-guard, rate limiting, audit). It does NOT relieve the operator of:

- **Configuring Odoo correctly.** The per-user ACL scoping the MCP relies
  on is enforced by Odoo, not by this MCP. Lax record rules, over-broad
  groups, or shared admin credentials defeat the safety case.
- **Reviewing `~/.odoo-mcp/suggestions.toml`** generated by `odoo-mcp
  onboarding`. The scan flags candidate sensitive fields; the operator
  decides which to redact.
- **Rotating API keys** regularly. Odoo does not enforce a key TTL, so
  this MCP records the set-date in the OS credential store and
  `odoo-mcp doctor` warns on keys older than 90 days. Adjust to your
  security policy via `rotation_warning_days` in `[defaults]` (default
  90) — for stricter regimes set lower; setting it to 0 effectively
  warns every run. Run `odoo-mcp setup --rotate-key NAME` to rotate the
  local half. Do this when a colleague leaves or a key is suspected
  compromised.
- **Reading SECURITY.md and the threat model.** Defaults are conservative
  but several can be loosened (`refuse_admin_on_production = false`,
  `allowed_models = ["..."]` strict mode, `custom_sensitive_field_patterns`
  exceptions). Each loosening is a deliberate choice with consequences
  the operator owns.
- **Securing the host.** A compromised laptop / workstation defeats every
  defence in this MCP. Standard endpoint hygiene (FileVault / BitLocker /
  LUKS, screen lock, OS updates) is the operator's responsibility.

## Limitations of liability

Provided as-is, no warranties, no fitness-for-purpose guarantees. See
[LICENSE](LICENSE) for the full disclaimer. The maintainers are not
liable for damages arising from use of this software.

## Reporting vulnerabilities

Email `security@deltix.pro`. PGP is available on request.

Please include:

- A description of the issue and which defense layer it bypasses.
- A reproducer or proof of concept, if you have one.
- The commit SHA you tested against.
- Whether you intend to disclose publicly and on what timeline.

We will acknowledge receipt within five business days and provide an
initial assessment within ten business days.

Please do not open public GitHub issues for security reports.

## Disclosure policy

We prefer coordinated disclosure. Default timeline: 90 days from the
date we acknowledge receipt to public disclosure, or sooner if we
ship a fix and confirm deployment with affected users. We are happy
to credit reporters in the changelog and release notes unless you ask
otherwise.

If a vulnerability is actively exploited in the wild, we reserve the
right to disclose and patch faster than the 90-day window.

## Supported versions

`odoo-mcp` is at `0.1.0`. Only the latest released version is
supported; there is no LTS branch. Security fixes will land on `main`
and in the next release. Users are expected to stay within one minor
version of the current release.
