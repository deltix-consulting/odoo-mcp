# Industry Audit — odoo-mcp v0.9.0

This document records the evidence-based survey of Odoo Community 18.0
that produced the v0.9.0 update to `MODEL_DENYLIST` and `_DEFAULT_HIDDEN`,
plus the four industry config templates under `templates/`.

It is a reference for deltix consultants doing klant onboarding — not a
runtime artifact.

## 1. Methodology

- **Source surveyed:** `github.com/odoo/odoo`, branch `18.0`, shallow
  clone taken at the time of the audit.
- **Module scope:** all of `addons/` plus the base modules under
  `odoo/addons/` (621 + 29 module folders, 1444 distinct `_name = "..."`
  model declarations).
- **Method:** grep for `_name = "..."` to build a full model inventory,
  then targeted `grep` on field declarations for credential / token /
  PII / financial keywords. Every entry added below has a citation back
  to a real file path in the Odoo source.

### Limitations

- **Odoo Enterprise modules are NOT covered.** `hr_payroll`,
  `account_consolidation`, `mrp_plm`, `documents`, `sign`, the German /
  Belgian / French localisation payroll packs, and many others live
  outside the public Community repo. Klanten on Enterprise must do
  their own pass for those modules and feed the findings back to deltix.
- **Custom Studio / OCA modules** are by definition out of scope. The
  per-instance `custom_sensitive_field_patterns` regex list in
  `config.toml` is the right tool for those.
- **Recent additions** to a klant's specific Odoo version may not be in
  `18.0` exactly. Re-run the audit when a klant moves to a newer version.

## 2. MODEL_DENYLIST additions

All 37 new entries below cite the source file in `addons/` or
`odoo/addons/` where the model is declared. Existing typo entries
(`auth_oauth.provider`, `auth_signup.reset.password`) were kept for
defense in depth and the corrected spellings added.

### Auth / user / credentials

| Model | Why blocked | Source |
| --- | --- | --- |
| `auth.oauth.provider` | OAuth client_id, endpoints, scopes (typo-corrected from existing entry) | `addons/auth_oauth/models/auth_oauth.py` |
| `auth.passkey.key` | WebAuthn / passkey credentials, sign_count | `addons/auth_passkey/models/auth_passkey_key.py` |
| `auth.totp.rate.limit.log` | 2FA attempt log — auth telemetry | `addons/auth_totp_mail_enforce/models/auth_totp_rate_limit_log.py` |
| `res.users.apikeys.show` | Transient wizard that echoes new API keys | `odoo/addons/base/models/` |
| `res.users.deletion` | Pending GDPR-style user deletion queue | `odoo/addons/base/models/res_users_deletion.py` |
| `res.users.settings` | Holds OAuth refresh tokens (Google / MS) via inherit | `odoo/addons/base/models/res_users_settings.py` |
| `res.users.settings.volumes` | Per-user voice-volume mapping (presence-adjacent) | `addons/mail/models/res_users_settings_volumes.py` |

### Mail-server / cross-system credentials

| Model | Why blocked | Source |
| --- | --- | --- |
| `ir.mail_server` | `smtp_user` / `smtp_pass` plaintext | `odoo/addons/base/models/ir_mail_server.py` |
| `fetchmail.server` | Incoming-mail credentials, OAuth tokens | `addons/fetchmail/models/fetchmail.py` |
| `mail.gateway.allowed` | Mail-routing bypass allowlist | `addons/mail/models/mail_gateway_allowed.py` |
| `google.gmail.mixin` | `google_gmail_refresh_token` storage | `addons/google_gmail/models/google_gmail_mixin.py` |
| `microsoft.outlook.mixin` | `microsoft_outlook_refresh_token` storage | `addons/microsoft_outlook/models/microsoft_outlook_mixin.py` |
| `google.service` | Google OAuth flow state | `addons/google_account/models/google_service.py` |
| `microsoft.service` | Microsoft OAuth flow state | `addons/microsoft_account/models/microsoft_service.py` |
| `google.calendar.sync` | Google sync state with tokens | `addons/google_calendar/models/google_sync.py` |
| `microsoft.calendar.sync` | Microsoft sync state with tokens | `addons/microsoft_calendar/models/microsoft_sync.py` |

### IAP (Odoo metered API) credentials

| Model | Why blocked | Source |
| --- | --- | --- |
| `iap.account` | `account_token` field (plaintext) | `addons/iap/models/iap_account.py` |
| `iap.service` | IAP service registry (referenced by accounts) | `addons/iap/models/iap_service.py` |

### Payment provider data (PCI scope)

| Model | Why blocked | Source |
| --- | --- | --- |
| `payment.token` | Tokenized cards / saved payment methods | `addons/payment/models/payment_token.py` |
| `payment.transaction` | Transaction history with bank refs | `addons/payment/models/payment_transaction.py` |
| `payment.provider` | Provider config, webhook URLs | `addons/payment/models/payment_provider.py` |
| `payment.method` | Payment method registry (joins tokens to providers) | `addons/payment/models/payment_method.py` |

### System / admin internals

| Model | Why blocked | Source |
| --- | --- | --- |
| `ir.default` | Default values across any model — write-side sneak | `odoo/addons/base/models/ir_default.py` |
| `ir.filters` | Saved searches (arbitrary domains) | `odoo/addons/base/models/ir_filters.py` |
| `ir.actions.act_url` | URL-redirect actions — phishing vector via write | `odoo/addons/base/models/ir_actions.py` |
| `ir.actions.todo` | Configuration-wizard queue | `odoo/addons/base/models/ir_actions.py` |
| `ir.embedded.actions` | Embedded action buttons | `odoo/addons/base/models/ir_embedded_actions.py` |
| `ir.asset` | Frontend JS / CSS assets — XSS via write | `odoo/addons/base/models/ir_asset.py` |
| `ir.profile` | Full SQL/Python stack-trace profiles | `odoo/addons/base/models/ir_profile.py` |
| `ir.cron.progress` / `ir.cron.trigger` | Cron-internal state | `odoo/addons/base/models/ir_cron.py` |
| `ir.module.category` | Module-mgmt internals | `odoo/addons/base/models/ir_module.py` |
| `ir.model.fields.selection` | Schema metadata | `odoo/addons/base/models/ir_model.py` |
| `ir.model.constraint` / `ir.model.relation` / `ir.model.inherit` | Schema metadata | `odoo/addons/base/models/ir_model.py` |
| `ir.exports` / `ir.exports.line` | Saved export specs (exfil) | `odoo/addons/base/models/ir_exports.py` |
| `bus.bus` / `bus.presence` | Real-time bus + presence (noise + privacy) | `addons/bus/models/bus.py`, `bus_presence.py` |

## 3. _DEFAULT_HIDDEN additions

All listed `(model, field)` pairs were verified against the Odoo 18.0
source. Many were already gated by `groups="hr.group_hr_user"` in Odoo
itself — that gate only protects from regular Odoo users, not from API
calls made with a privileged user, which is the threat model the MCP
operates under.

| Model | Fields added | Source |
| --- | --- | --- |
| `res.partner` | `comment`, `barcode` | `odoo/addons/base/models/res_partner.py` |
| `res.partner.bank` | `acc_number` (full IBAN) | `addons/account/models/res_partner_bank.py` |
| `account.journal` | `bank_acc_number` | `addons/account/models/account_journal.py` |
| `account.payment` | `memo` | `addons/account/models/account_payment.py` |
| `hr.employee` | `sinid`, `passport_id`, `permit_no`, `visa_no`, `visa_expire`, `private_street`, `private_street2`, `private_city`, `private_state_id`, `private_zip`, `private_country_id`, `private_car_plate`, `gender`, `emergency_contact`, `emergency_phone`, `study_field`, `study_school`, `km_home_work`, `bank_account_id`, `barcode` | `addons/hr/models/hr_employee.py` |
| `hr.contract` | `wage`, `contract_wage`, `notes` (wage not caught by always-redacted regex) | `addons/hr_contract/models/hr_contract.py` |
| `hr.applicant` | `email_from`, `partner_phone`, `partner_phone_sanitized`, `linkedin_profile`, `refuse_reason_id` | `addons/hr_recruitment/models/hr_applicant.py` |
| `hr.candidate` | `email_from`, `partner_phone`, `partner_phone_sanitized`, `linkedin_profile` | `addons/hr_recruitment/models/hr_candidate.py` |
| `hr.leave` | `private_name`, `notes` | `addons/hr_holidays/models/hr_leave.py` |
| `hr.expense` | `description` (Internal Notes) | `addons/hr_expense/models/hr_expense.py` |
| `fleet.vehicle` | `license_plate`, `vin_sn`, `description` | `addons/fleet/models/fleet_vehicle.py` |
| `calendar.event` | `videocall_location`, `access_token` | `addons/calendar/models/calendar_event.py` |
| `calendar.attendee` | `access_token` | `addons/calendar/models/calendar_attendee.py` |

## 4. Per-industry guidance

### Wholesale / Distribution
- **Typical models in scope:** `product.template`, `product.product`,
  `stock.picking`, `stock.move`, `stock.quant`, `purchase.order`,
  `sale.order`, `account.move`, `delivery.carrier`.
- **Sensitive fields to be aware of:** supplier cost data, customer
  pricing tiers, margin calculations (often Studio fields).
- **Recommended template:** `templates/wholesale.toml`.

### Manufacturing
- **Typical models in scope:** `mrp.bom`, `mrp.production`,
  `mrp.workorder`, `quality.check`, `maintenance.request`, plus the
  wholesale backbone.
- **Sensitive fields to be aware of:** quality-check incident notes,
  BoM cost rollups, supplier-specific recipes / formulas.
- **Recommended template:** `templates/manufacturing.toml`.

### HR-heavy
- **Typical models in scope:** `hr.employee`, `hr.contract`,
  `hr.applicant`, `hr.candidate`, `hr.leave`, `hr.expense`,
  `hr.attendance`. Plus `hr_payroll` if on Enterprise.
- **Sensitive fields:** every employee identifier, all wage / contract
  data, all candidate contact info, time-off reasons.
- **Recommended template:** `templates/hr.toml`. Add custom sensitive
  fields once you discover the klant's payroll-module field names.

### Professional services
- **Typical models in scope:** `project.project`, `project.task`,
  `account.analytic.line` (timesheets), `helpdesk.ticket`,
  `account.move`, `sale.order`.
- **Sensitive fields:** task / ticket descriptions can hold customer
  context, sometimes pasted credentials. Invoice narration too.
- **Recommended template:** `templates/professional-services.toml`.

## 5. What was deliberately left alone

- **`mail.activity`** — activity reminders are usually short, generic,
  and not a privacy hot spot. If a klant uses activities to track
  sensitive HR cases, that's a per-klant override.
- **`crm.lead.description`** — sales notes are intentionally readable so
  the agent can do pipeline analysis; klanten that put NDA-grade detail
  in lead descriptions should add `crm.lead` to their override.
- **`account.move.narration`** — kept readable for the
  professional-services template only; the global default leaves it
  open. Klanten with confidential invoice notes should override.
- **`hr.employee.address_id`** — the public address linkage. The
  *private* address is hidden; the public business address linkage is
  not, by design.
- **All `*.report.*` models** — financial reports are derived data, not
  primary records. The underlying primary records they read from are
  already covered.
- **`im_livechat.channel`, `discuss.channel`** — chat channels can hold
  sensitive transcripts but are extremely klant-specific. Left to the
  per-instance override mechanism.

## 6. Per-klant scan (v0.10+)

The audit above describes the **standard** Odoo Community 18.0 surface.
Every klant has Studio fields, OCA modules, and bespoke custom modules
that we cannot anticipate from upstream alone. Use `scan-custom` after
onboarding any new klant to discover that custom surface:

```bash
odoo-mcp scan-custom <instance>             # human-readable report
odoo-mcp scan-custom <instance> --toml      # paste-ready config snippet
odoo-mcp scan-custom <instance> --json      # for scripting
```

The command authenticates as the configured MCP user, enumerates every
model and field via `ir.model.search_read` + `fields_get`, diffs against
the embedded Odoo 18.0 reference (`src/odoo_mcp/_odoo_reference.py`),
and classifies each non-standard field on sensitivity. Each finding gets
one of: `BLOCKED` (already covered by built-in always-redacted regex),
`GATED` (already in `_DEFAULT_HIDDEN`), `LIKELY_SENSITIVE` (name or
help-text matches a PII / confidentiality keyword — Dutch / Flemish
included for BE klanten: `loon`, `geboorte`, `vertrouwelijk`,
`persoonlijk`, `rijksregister`, `geslacht`, `burgerlijk`),
`LIKELY_FINANCIAL` (numeric type + financial keyword),
`BINARY_AUTO_STRIPPED` (informational), or `UNCERTAIN` (requires
manual review).

Worked example from a fictional klantx:

```text
== Custom fields on standard models ==
  hr.employee  [3 custom field(s)]
    x_studio_salary_grade   [many2one ] BLOCKED          — already covered by built-in policy
    x_loon_groep            [selection] LIKELY_SENSITIVE — name contains 'loon'
    x_klantx_pin            [char     ] UNCERTAIN        — review manually
```

Then `--toml` produces a snippet you can paste under `[instances.klantx]`.

The command **deliberately bypasses the dispatcher denylist**. It is
admin tooling operated by the consultant, not a Claude tool call. The
denylist exists to constrain Claude, not the operator.

## 7. Rerunning the audit

When Odoo ships a new major version, the methodology in section 1 is
the playbook. The high-leverage searches are:

```bash
# All models
grep -rh "_name = " --include="*.py" addons/ odoo/addons/ \
  | grep -oE "_name = ['\"][a-z][a-z0-9_.]+['\"]" | sort -u

# Credential / token / api-key fields
grep -rh "fields\." --include="*.py" addons/ \
  | grep -iE "(refresh_token|access_token|api_key|client_secret|smtp_pass|webhook|sign_count)"

# HR sensitive fields
grep -rh "fields\." addons/hr*/ --include="*.py" \
  | grep -iE "(passport|visa|private_|ssnid|sinid|emergency|spouse|birth|nationalit|marital|study)"
```

Update this document with the new findings, bump
`MODEL_DENYLIST` / `_DEFAULT_HIDDEN`, and ship a new version.

After bumping `MODEL_DENYLIST` / `_DEFAULT_HIDDEN`, regenerate the
embedded standard-model reference used by `scan-custom`:

```bash
git clone --branch <new-major> --depth 1 \
  https://github.com/odoo/odoo.git /tmp/odoo-audit/odoo
uv run python scripts/regen_odoo_reference.py --version <new-major>
```

This rewrites `src/odoo_mcp/_odoo_reference.py`. Commit it alongside the
audit changes; the file is auto-generated but committed so the MCP has
zero runtime dependency on Odoo source.
