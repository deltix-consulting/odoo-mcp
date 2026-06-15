# Changelog

All notable changes to `odoo-mcp` will be documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While we are still on 0.x the public API (tool names, argument shapes,
config schema) may change between minor versions; we will call out any
breaking change explicitly in this file.

## [Unreleased]

## [0.24.0] - 2026-06-15

### Added

- **`odoo_create_attachment` now accepts ``source_path`` for
  server-side file reads.** Real-world failure: several agent SDKs
  silently drop a turn when the inline base64 in a tool-call grows
  past ~5 KB. A ~2.5 MB invoice PDF becomes ~3.3 MB of base64 in
  the tool input and the turn dies without an error — the v0.23.0
  25 MB cap was misleading because the SDK cliff hits long before
  we get there. Worse, the agent often runs in an ephemeral
  sandbox that can't even read the downloaded file to base64-
  encode it; the bytes live on the api-server disk, not in the
  agent context.

  Fix: an optional ``source_path`` argument that the MCP reads
  + base64-encodes server-side. The downloader writes a file to a
  known path, hands the path to the agent, the agent passes
  ``source_path`` to ``odoo_create_attachment``, and no bytes
  traverse the agent context at all. ``datas_base64`` stays for
  small inline payloads; exactly one of the two is required.

  Security envelope:

  - **Opt-in via TOML allowlist.** New per-instance config field
    ``attachment_source_paths = ["/abs/dir", ...]``. Default is
    empty — ``source_path`` is then refused outright. Operators
    list the specific directories the MCP is allowed to read
    from; everything else (``/etc``, ``~/.ssh``, etc.) stays
    unreachable.
  - **Path must be absolute.** Relative paths would resolve
    against the MCP process's CWD, which is operator-confusing
    and a footgun.
  - **realpath-based containment check.** The submitted path is
    resolved via ``os.path.realpath`` (so symlinks are followed
    once) and matched against the (also-realpath'd, at config-
    load time) allowlist. A symlink that lives inside the
    allowed directory but points at a file outside is refused —
    the test suite pins this attack case explicitly.
  - **Regular files only.** Devices, FIFOs, sockets, directories
    are refused at the ``stat`` step.
  - **Size cap via stat BEFORE read.** No 50 GB file is opened
    just to fail. A second size check after read closes a TOCTOU
    edge case if the file grew between stat and read.
  - **Payload-digest binding still works across modes.** After
    reading, the dispatcher canonicalises the args (drops
    ``source_path``, injects ``datas_base64``) before the prod-
    guard computes the digest. A preview with ``source_path``
    and a commit with the same bytes — via either input mode —
    share a token. A content swap between preview and commit
    (different file at the same path, different path with
    different bytes) is refused by the digest check, just like
    the v0.23.0 inline path.

  10 new tests, 762 total (was 752). Cover: end-to-end multi-MB
  PDF read + commit, default-deny when allowlist empty, refusal
  outside allowlisted dirs, symlink-escape refusal, relative-path
  refusal, directory refusal, over-cap refusal, both inputs
  given, neither input given, and content-swap detection via
  payload digest when both calls use ``source_path``.

## [0.23.0] - 2026-06-12

### Added

- **New tool `odoo_create_attachment`** — bounded write path for
  `ir.attachment`. Inputs: `instance`, `res_model`, `res_id`,
  `filename`, `datas_base64`, optional `mimetype` + `description`.
  Attaches a base64-encoded file to an Odoo record.

  `ir.attachment` itself **stays on the global denylist** — the
  agent cannot `search_read` arbitrary attachments (real exfil
  risk, since attachments often carry sensitive PDFs that bypass
  record-rules), cannot `write`, cannot `unlink`. The new tool is
  the single permitted operation: create-only, with the user-
  facing `res_model` validated through the standard `check_model`
  pipeline (allowlist + denylist + write-blocklist) so attaching
  to e.g. `ir.model` or `res.users` is refused. Decoded size
  capped at 25 MB. Filename must contain no path separators.
  Target record must exist (one `search_count` round-trip) —
  orphan attachments are refused.

  Full prod-guard pipeline: dry-run preview returns the file
  metadata + a `confirmation_token`, commit consumes the token.
  The token's payload digest binds to the exact file bytes (the
  v0.18.0 token-binding fix applied here) so an agent that dry-
  runs a small placeholder cannot commit a different file with
  the same token.

  13 new tests cover: write-op classification, tool registration,
  dry-run preview + metadata, data-URL prefix tolerance, end-to-
  end create → ir.attachment row, payload-digest content-swap
  rejection, denylist refusal, write-blocklist refusal, filename
  path-separator rejection, invalid-base64 rejection, over-size
  cap rejection, nonexistent-target-record rejection, and a
  defense-in-depth check that `ir.attachment` remains denylisted.

## [0.22.0] - 2026-06-12

Efficiency release. Driven by an audit of 2,794 real-world tool calls
(10.6% failure rate): the dominant cost of an agent session is the
number of calls and failures, not per-call latency. Every change below
removes a failure class or a forced round-trip observed in the field.

### Fixed

- **Domain sandbox: implicit-AND domains accepted.** The polish-arity
  validator rejected Odoo-valid domains mixing implicit AND with
  explicit operators (e.g. `[leaf, '|', leaf, leaf]`) with "Malformed
  domain: expected a single top-level expression". Odoo's
  `normalize_domain` joins leftover top-level expressions with an
  implicit AND; the sandbox now does the same. Underfed `& | !`
  operators are still rejected.
- **Help cookbook no longer teaches a rejected pattern.** The first
  `common_patterns` example used a dotted domain
  (`stage_id.name`) that the sandbox itself refuses — the most
  frequent domain rejection in audit logs traced back to agents
  copying it. Replaced with a sandbox-valid example; a new test pins
  every cookbook domain to pass `sandbox_domain`.
- **Transport: failed requests no longer poison the keep-alive
  connection.** A timeout or protocol error mid-request left the
  cached HTTP connection in the `Request-sent` state, failing every
  subsequent call with `ResponseNotReady` until restart. Both
  transports now drop the cached connection on any request failure.

### Changed

- **Prod-guard unlock renews in place.** Calling
  `odoo_enable_prod_writes` while a window is still active now resets
  the expiry and commit budget but keeps the window identity, so
  confirmation tokens issued before the renewal stay consumable —
  hitting the burst limit mid-batch no longer forces re-doing
  reviewed dry runs. An *expired* window still gets a fresh identity
  (stale tokens never survive a real expiry).
- **`odoo_diagnose_access` reports MCP-blocked models instead of
  failing on them.** For a model blocked by the denylist or a
  strict-mode allowlist it returns `mcp_blocked: true` with the
  reason and the config key to change (`allowed_models` in
  `~/.odoo-mcp/config.toml`) — previously it raised the very error
  the caller was trying to diagnose. Permitted models additionally
  report `write_blocked_via_mcp`.
- **`ModelNotAllowedError` hint is actionable** — names
  `odoo_diagnose_access` and the `allowed_models` config key instead
  of "contact your MCP administrator".

### Added

- **`res.users` readable through a hard identity-field whitelist.**
  Resolving `user_id` to a name/login/email is the most common
  relational lookup in CRM flows; the full denylist entry produced a
  steady stream of dead-end calls (43 in the audited logs, plus the
  `user_id.login` dotted-domain workaround attempts it provoked).
  Reads now expose exactly `id, name, display_name, login, email,
  active, partner_id, company_id, lang, tz, share` — enforced at the
  dispatcher's single `fields_get` choke point
  (`restrict_fields_meta`), so describe/smart-fields/explicit
  `fields=`/domains/responses all see the same truncated model.
  Auth state (password, TOTP, API keys, `groups_id`) and the
  inherited `res.partner` PII surface stay invisible; every write
  path is refused via `MODEL_WRITE_BLOCKLIST` regardless of
  prod-guard state. Tests pin the whitelist contents.
- **Logistics smart-field defaults.** Default reads on
  `sale.order(.line)`, `product.template/product`,
  `stock.warehouse/picking/move/rule/route` now include the
  behavior-deciding routing fields (`route_id(s)`, `delivery_steps`,
  `picking_type_id`, `rule_id`, ...) that the generic heuristics
  dropped as heavy types or crowded out past the 25-field cap. An
  agent comparing a good and a bad sale order can now SEE the
  routing difference in default reads.
- **Cookbook patterns for the two workflows agents kept failing:**
  tracing a relation in two calls (dotted domains are rejected; batch
  many2one resolution with one `odoo_read`, never one call per id)
  and explaining an unexpected transfer type via
  `odoo_diagnose_routing` + the route-precedence checklist.
  `odoo_diagnose_routing` is now listed in `odoo_help` (it was
  missing entirely). The terse-help size budget moved 1700 -> 1900
  chars to fit the 16th tool entry.


## [0.21.0] - 2026-06-12

### Added

- **New tool `odoo_diagnose_routing`** — read-only inspector that
  answers "which `stock.rule` and `picking_type_id` could fire for
  this (product, warehouse) pair?". Built in response to a real
  field incident: a deltix SO produced a `TR-LAAD` picking
  (scenario B) instead of the expected `LAAD` (scenario A), and the
  agent investigating it had no way to introspect
  `stock.route` / `stock.rule` from inside its sandbox.

  Inputs: `instance`, `product_id`, `warehouse_id`.
  Returns: the product + its template, the warehouse config
  (delivery/reception steps, `sale_route_id`, `mto_pull_id`),
  every candidate `stock.route` reachable from the product or
  warehouse, and every candidate `stock.rule` on those routes
  matching the warehouse (or wildcard) — each rule with its
  source/destination locations, `picking_type_id`, action,
  procure-method, and sequence.

  Deliberately does NOT predict the winning rule. Odoo's runtime
  rule resolution involves sequence, location-chain matching, MTO
  chains, and custom overrides from installed modules; replicating
  that client-side would drift on every Odoo release. The tool's
  response is the candidate set + a note that calls out the
  no-prediction guarantee — and a test pins the absence of any
  `winning_rule` / `predicted_picking_type` field so a refactor
  can't quietly add prediction back.

  Allowlist bypass for six operator-configuration models
  (`product.product`, `product.template`, `stock.warehouse`,
  `stock.route`, `stock.rule`, `stock.location`) — none carry
  business data, and a test pins the model set so future changes
  can't silently widen it.

  Eight new tests cover: read-op classification, tool registration
  order, happy-path candidate enumeration, the non-prediction
  guarantee, the six-model touch limit, allowlist-bypass without
  config changes, and honest failure on unknown product or
  warehouse ids.

## [0.20.0] - 2026-06-12

### Added

- **`odoo_run_document_action` now supports cancel for four more
  models**, broadening AI-driven cancel-workflow coverage from sales
  + purchase + invoicing + delivery into manufacturing, payments
  and HR. Each new row goes through the same dry-run + confirmation
  token + payload-digest pipeline as the existing actions:

  | model | action | Odoo method |
  |---|---|---|
  | `mrp.production` | `cancel` | `action_cancel` |
  | `account.payment` | `cancel` | `action_cancel` |
  | `hr.leave` | `cancel` | `action_cancel` |
  | `hr.expense.sheet` | `cancel` | `action_cancel` |

  Deliberately NOT exposed (each is its own row in the test pin so a
  future "let's just add this" PR has to delete the test and explain
  why):

  - `hr.leave` *refuse* — manager-side rejection, an HR decision
    that should go through the UI not an agent.
  - `hr.expense` (singular) *cancel* — Odoo manages expense state
    through the parent sheet; per-line cancel leaves the sheet
    inconsistent.

  Two new tests pin the additions, and one inverse-coverage test
  pins the exclusions.

## [0.19.1] - 2026-05-29

### Changed

- **Dry-run previews now surface ``commits_remaining`` and a one-line
  no-cost guarantee.** Real-world failure: an agent hit the burst
  limit on production writes and reported "counts dry-runs too" —
  WRONGLY, because dry-runs never decremented. The actual code was
  correct; the bug was UX. Dry-run responses carried no
  ``commits_remaining`` field at all, so the agent had no
  observation that would refute its incorrect prior, and
  defensively shrank its batch size ("switching to one group of 5
  per unlock") — wasting throughput the budget was designed to
  allow.

  Every preview now includes:

  - ``commits_remaining`` — the unchanged budget value, identical
    to what the commit path returns, so an agent doing ten
    consecutive dry-runs can SEE the counter holds steady.
  - ``commits_remaining_note`` — text payload spelling out
    "This is a dry-run; commits_remaining is unchanged. Only a
    successful commit decrements the burst budget."

  The ``ProdGuardError`` raised when the budget is actually
  exhausted now also says "Dry-runs do NOT count toward this
  budget; only successful commits do" — so even an agent that
  never read the no-cost note gets the explanation at failure time.

  Both fields are prod-only — non-prod previews omit them so dev
  users aren't misled into thinking the budget applies on their
  sandbox. Three new dispatcher-level tests pin the preview
  contract (present on prod, absent on dev, never moves across
  repeated dry-runs); one prod_guard test pins the error-message
  language.

## [0.19.0] - 2026-05-21

### Added

- **``odoo-mcp setup --scheduler-config [--format=json|env|cli]``** —
  print the snippet a non-Claude scheduler (Decisions, n8n, custom
  cron) needs to load odoo-mcp. Triggering scenario: a scheduled
  cron job ran with no MCP wired up, the spawned agent reported
  "Odoo MCP niet geladen" and no facturen got checked. We can't
  auto-write into an arbitrary scheduler's config — there's no
  shared format — but we can emit the exact snippet the operator
  pastes, with the host-specific absolute path to ``odoo-mcp``
  resolved locally so it matches the install.

  Three formats:

  - ``json`` (default): the ``{"mcpServers": {"odoo-mcp": ...}}``
    shape understood by Claude Desktop, the Anthropic CLI, and
    most MCP-aware schedulers.
  - ``env``: shell-style ``KEY=VALUE`` for schedulers that read
    MCP config from environment instead of a structured file.
  - ``cli``: bare ``<command> launch`` string for shell-based
    schedulers that just spawn a stdio process.

  The snippet goes to stdout (so it can be redirected straight
  into a file); the instructions and verification hint go to
  stderr. Five new tests cover all three formats, an explicit
  rejection of unknown formats (so a typo doesn't silently fall
  back to JSON), and the ``--format env`` space-separated
  invocation in addition to ``--format=env``.

## [0.18.2] - 2026-05-21

### Changed

- **Admin-refusal error now names concrete safe groups and the
  prompt-injection risk of opting out.** Observed in the field:
  operators hitting the "Refusing to use admin credentials" error
  often re-created their "MCP Agent" Odoo user with the Settings
  group again, because the error said "grant only the groups it
  needs" without naming any. The new message:

  - **Lists a safe starter set** — Sales (User), CRM (User),
    Inventory (User), Accounting (Billing) — and explicitly tells
    the operator NOT to grant Settings / Administration.
  - **Names the risk of opt-out** in plain language: setting
    ``refuse_admin_on_production = false`` means "one
    prompt-injection can then write anywhere". The previous
    "NOT recommended" phrasing read like routine config advice.

  Two new tests pin the safe-group list and the prompt-injection
  language so a future refactor can't quietly weaken the message.

## [0.18.1] - 2026-05-21

### Fixed

- **The XML-RPC transport now honors ``HTTPS_PROXY`` / ``NO_PROXY``.**
  Python's stdlib ``xmlrpc.client`` — unlike ``urllib.request`` —
  does NOT pick up ``HTTPS_PROXY`` from the environment. Our
  ``_TimeoutSafeTransport`` was a thin timeout-adding subclass, so
  it inherited the same gap. Containers running odoo-mcp behind a
  Squid + iptables egress allowlist (the now-standard tenant shape)
  silently 30-second-timed-out on every authenticate / call: the
  Bun-side ``odooAuthenticate`` went through the proxy and worked
  (HTTP 200 in 53ms), the Python child went direct, hit the
  ``DOCKER-USER -j DROP`` rule, and died.

  ``_TimeoutSafeTransport`` now consults ``HTTPS_PROXY`` /
  ``https_proxy`` (lowercase wins, matching urllib), honors
  ``NO_PROXY`` as a comma-separated suffix list including ``*``
  and leading-dot variants, and CONNECT-tunnels through the proxy
  via ``HTTPSConnection.set_tunnel``. The TLS handshake still
  terminates at the Odoo host — certificate validation continues
  to verify the Odoo cert, not the proxy's. Authenticated proxies
  (``http://user:pass@squid:3128``) are supported via
  ``Proxy-Authorization: Basic …`` headers on the tunnel.

  Twelve new tests pin the env-var resolution (``HTTPS_PROXY`` vs
  ``HTTP_PROXY`` per target scheme, lowercase precedence, suffix
  matching, ``*`` short-circuit, basic-auth extraction, bare
  ``host:port`` tolerance) and the transport integration (tunnel
  set when proxy configured, direct when not, ``NO_PROXY``
  beats ``HTTPS_PROXY`` at the transport level too).

  HTTP-via-HTTP-proxy is intentionally still unsupported — every
  reported affected setup is Odoo Online (HTTPS) behind Squid.

## [0.18.0] - 2026-05-21

### Security

- **Confirmation tokens are now bound to the previewed payload, not
  just (instance, op, model).** Previously an agent that ran a dry
  run for ``odoo_write(ids=[1], values={"active": false})`` and
  received a confirmation token could re-call the commit with the
  same token and ``ids=[1, 2, ..., 1000]``: the prod-guard's check
  validated (instance, op, model) only, so the wider payload slipped
  through and 1000 records were updated under the cover of an
  approval the operator gave for one. Same attack shape for
  ``values`` on create / write, ``partner_ids`` / ``body`` on
  send_message, ``action`` on run_document_action, and id-count
  upgrades on archive_or_delete (note: archive↔delete *mode* swaps
  were already caught one layer earlier by the (op) check, since
  they map to different ``Operation`` enums).

  ``ProdGuard.create_pending`` and ``consume_pending`` now take an
  optional ``payload_digest`` (canonical SHA-256 over the previewed
  payload — see ``compute_payload_digest``). The dispatcher computes
  the digest at preview time, stores it on the pending token, and
  re-computes it from the current call's args at commit time. Any
  drift in the payload-bound keys raises
  ``"Confirmation token was issued for a different payload"`` and
  refuses the commit. Tokens issued without a digest (the unit-test
  path that calls ``consume_pending`` directly) keep working —
  ``payload_digest=None`` opts out of the check.

  Surfaced by the daily competitive audit
  (``audit/2026-05-21-payload-digest-token-binding``): the same
  attack shape — same fix shape — landed in AlanOgic/odoo-mcp-19 as
  "Bind confirmation tokens to operation payload digest" on
  2026-05-01.

  16 new tests cover: canonical digest stability across key
  reordering, all five payload-bound attack vectors (extra ids on
  write, swapped values on create, added partner on send_message,
  swapped action on document_action, id-count upgrade on delete),
  the defence-in-depth case (archive→delete still caught by the op
  check), the happy path, and a regression that the new error
  message does not leak the token literal.

## [0.17.8] - 2026-05-20

### Fixed

- **Diagnose unrecognised ``make_key`` return shapes instead of
  failing opaquely.** v0.17.7's web JSON-RPC flow gets past
  ``@check_identity``'s HTTP-only gate, but on at least one Odoo
  Online tenant ``make_key`` returns an action shape we don't
  recognise — and the previous "unrecognised shape" error gave no
  way to diagnose without leaking the key value in a bug report.
  Three changes:

  1. **Identity-check redirect detection.** When ``make_key``
     returns ``ir.actions.act_window`` pointing at
     ``res.users.identitycheck`` (Odoo's "please re-type your
     password" wizard, opened by the browser when the in-session
     identity stamp isn't fresh enough), surface a specific
     message explaining what happened and how to recover, rather
     than the generic shape error.
  2. **Wider key-extraction probes.** Accept the key at the top
     level of the action (``action["key"]``) and inside
     ``action["params"]["key"]`` — two shapes observed in the
     wild — in addition to the canonical
     ``action["context"]["default_key"]``.
  3. **Redacted shape summary on the failure path.** When the
     extractor still can't find the key, the error now includes a
     one-line shape summary (type / res_model / top-level keys /
     context keys — NEVER any values) so the maintainer can
     diagnose from a bug report without the user having to copy
     the key (which would defeat the point of the OS keychain).

  Five new tests pin all three behaviours: identity-check
  detection by ``res_model`` and by action ``name``, the wider
  extractor probes, and a guarantee that the shape summary never
  leaks field values.

## [0.17.7] - 2026-05-20

### Fixed

- **Password-based API-key generation now actually works against
  Odoo Online and any Odoo ≥17.** v0.17.6 switched from
  ``_generate`` to the public ``make_key`` wizard, but Odoo ≥17
  decorates ``make_key`` with ``@check_identity`` from
  ``auth_totp``. That decorator demands a real HTTP ``request``
  object — XML-RPC has none — so the call still failed with:

  ```
  Odoo refused to generate a new key: Deze methode is alleen
    toegankelijk via HTTP
  ```

  (English: ``This method can only be accessed over HTTP``.) The
  fix abandons XML-RPC entirely for this command and drives Odoo's
  **web JSON-RPC** layer instead, exactly as a browser does:

  1. ``POST /web/session/authenticate`` — establishes the session
     cookie AND stamps ``identity-check-last``, satisfying
     ``@check_identity`` for the next ~10 minutes.
  2. ``POST /web/dataset/call_kw`` — drives search + unlink for
     cleanup, then creates the description wizard record, then
     calls ``make_key`` on it. All four calls share the same
     cookie jar so the session is consistent.
  3. Extract the new key from the returned action's
     ``context.default_key`` (unchanged from v0.17.6).

  Side benefits: no SSL/XML-RPC dual-proxy setup, simpler error
  shape (everything is JSON), and the auth-failure path now
  surfaces the actual Odoo error message rather than an XML-RPC
  fault code.

  The HTTP-only error path also gets its own actionable formatter
  in case it surfaces again behind a cookie-stripping proxy or
  expired session — three new tests pin the messages so a future
  refactor can't quietly regress them.

## [0.17.6] - 2026-05-20

### Fixed

- **Password-based API-key generation now actually works against
  stock Odoo, including Odoo Online.** v0.16.4 through v0.17.5
  attempted ``res.users.apikeys._generate`` directly over XML-RPC.
  Odoo's RPC dispatcher unconditionally rejects any method whose
  name starts with ``_`` ("Private methods … cannot be called
  remotely"), so this path was broken on every stock Odoo from day
  one — it only ever succeeded on instances with a custom addon
  exposing the method. Real-world breakage:

  ```
  Choice [2]: 2
  Odoo password (will not echo, not stored):
  Odoo refused to generate a new key: Private methods (such as
    'res.users.apikeys._generate') cannot be called remotely.
  ```

  Both ``setup --add`` (option 2) and ``renew-key`` now drive
  Odoo's own user-facing wizard instead — the same path the
  Account-Security UI takes:

  1. ``create`` a ``res.users.apikeys.description`` record with the
     desired name (transient model, auto-GC'd by Odoo's vacuum).
  2. Call ``make_key()`` on it (no leading underscore → RPC-callable).
  3. Extract the new key from the returned action's
     ``context.default_key``.

  Three failure modes get explicit, actionable error messages:
  ``Private methods…`` (stale client → upgrade), ``@check_identity``
  rejection on Odoo ≥17 (manual creation required), and any other
  fault (manual creation + paste). New tests pin:

  - that no underscore-prefixed method is ever sent over RPC,
  - that each of the three fault paths produces the right message,
  - that all three known ``make_key`` return shapes (act_window with
    ``default_key``, with ``default_key_value``, and the legacy raw
    string) are decoded — and that an unrecognised shape raises
    rather than writing rubbish to the OS keychain.

## [0.17.5] - 2026-05-20

### Fixed

- **`odoo-mcp update --check` no longer lies "Up to date" when it
  couldn't reach GitHub.** Real-world failure: a v0.15.10 user behind
  a corporate NAT hit the anonymous GitHub API rate limit; the fetch
  returned None and `--check` cheerfully printed
  ``Up to date (version 0.15.10)`` — hiding exactly the failure mode
  that v0.17.4 set out to fix. The output now distinguishes:

  - fetch failed → yellow message on stderr ("Could not reach
    GitHub…") plus a tip about `gh auth login`, exit code 1.
  - newer release exists → ``Update available: X (you have Y).``
  - already current → ``Up to date (version Y).``

  Three new tests pin each branch so a future refactor can't quietly
  reintroduce the silent-failure behaviour.

## [0.17.4] - 2026-05-20

### Security

- **`odoo-mcp update` now uses the authenticated `gh` CLI to fetch the
  latest release tag, falling back to anonymous `urllib` only when
  `gh` is unavailable.** Before this change, every update attempt hit
  GitHub's unauthenticated API rate limit (60 requests/hour shared per
  IP — trivially exhausted behind a corporate NAT, by an over-eager
  update loop, or just bad luck). When the rate-limit hit, the updater
  printed:

  > Warning: could not determine latest release tag (GitHub API
  > unreachable). Attestation not verified. Proceed without
  > verification? [y/N]:

  Every user we observed pressed `y`. That turned the Sigstore-backed
  build-provenance attestation check — the whole point of the verified
  update path — into security theater. The installer already requires
  `gh auth login`, so the authenticated path (5000 req/hour) is the
  realistic case; users without `gh` keep the historical fallback and
  are no worse off than before.

  Two new tests pin the behaviour: `gh` is preferred when present, and
  a non-zero `gh` exit falls back to `urllib`. Existing urllib-mocked
  tests were updated to also stub `shutil.which` so they exercise the
  intended fallback path.

## [0.17.3] - 2026-05-16

### Security

- **`InstanceNotFoundError` now actively discourages the AI from
  substituting a different instance.** Real-world failure mode that
  triggered this: a user asked Claude to "search in the Odoo demo
  environment". The MCP install had only ``prod`` configured. The AI
  saw the previous error ("Known instances: ['prod']") and
  "helpfully" picked ``prod`` on its own — reading production data
  the user had explicitly NOT asked for. On a write path the same
  pattern would have triggered a prod-write preview against the
  wrong dataset; on a read path it's a data-exposure incident.

  The hint returned alongside the error now reads (verbatim):

  > STOP — do not silently retry this call against a different
  > instance. The user named an instance that does not exist on this
  > machine; ask the user which instance they meant. Do NOT
  > substitute another real instance (especially production) based on
  > similarity or guess.

  The factual message still lists configured instances so a human
  with a typo can self-correct from the audit log or client UI.
  The behavioural directive lives in the hint, which the dispatcher
  surfaces separately on every error response.

  Four new tests pin the hint phrasing (``STOP``, "do not retry",
  "substitute", "ask the user", "production"), the end-to-end shape
  of a read call against an unknown instance, the write path
  (refused at instance lookup, BEFORE any prod-guard logic, with the
  hint visible), and the empty-instance-name path.

  No code-path change beyond the error message — the dispatcher
  refused unknown instances before and still does. The change is in
  what we tell the AI to do with that refusal.

## [0.17.2] - 2026-05-16

### Changed

- **`renew-key` (and the setup wizard's "generate key for me" path)
  now cleans up the previous key before generating a new one.**
  Without this, daily renewal on Odoo Online (1-day expiry policy)
  accumulated one stale-but-expired API-key row per day in the user's
  Odoo profile. After a year: 365 dead rows.

  The fix:

  - Stable per-install key name: `odoo-mcp (<instance>) on <hostname>`.
    Visible in Odoo profile → Account Security so the user knows which
    key belongs to which laptop.
  - Before generating a new key, search the user's own
    `res.users.apikeys` for rows with that exact name and `unlink` them.
  - Generate the new key (same name).
  - Result: at most **one** MCP key per (user, instance, machine) in
    Odoo at any time.

  Cleanup is **best-effort**: an XML-RPC fault or network error during
  the cleanup logs a warning and lets the renewal continue. A user who
  cannot delete their own apikey rows (custom ACL) still gets the new
  key; old rows accumulate as before until the ACL is fixed.

  Cleanup on **other machines** is never touched — the hostname suffix
  scopes the deletion to this install only. Manually-created keys with
  different names are also untouched.

  Onboarding implication: on first install nothing changes (search
  returns 0 rows). On existing installs the FIRST renewal after this
  release may unlink the prior `odoo-mcp (<instance>)`-named row only
  if it happens to match the new format on this exact hostname; older
  rows from before this release will be silently kept. Operators who
  want a clean slate can delete them once via the Account Security UI.

### Internal

- `_generate_api_key_via_password()` now returns
  `tuple[str, int]` — `(new_key, num_cleaned_up)`. Both callers
  (`renew-key` CLI and `setup` wizard option 2) surface the cleanup
  count when non-zero.
- New `_mcp_key_name(instance)` helper centralises the
  `odoo-mcp (<instance>) on <hostname>` convention.

## [0.17.1] - 2026-05-16

### Changed

- **Clearer error when authentication returns no uid.** On Odoo Online,
  non-admin API keys expire after 1 day; an expired key fails
  `authenticate()` exactly like a wrong key (Odoo returns no uid for
  both). The error message now names that likely cause and gives the
  exact fix — `odoo-mcp renew-key <instance>` — instead of the generic
  "check the username, API key, and database name". No behaviour
  change, just a message a non-expert can act on.

## [0.17.0] - 2026-05-16

Driven by real production feedback: a user created a purchase order in
draft via `odoo_create` and then needed it confirmed so the transport
planner could work. The MCP could not — it had no way to trigger an
Odoo workflow action. This release adds that, narrowly.

### Added

- **`odoo_run_document_action` tool.** Runs a document workflow action
  — `confirm` / `cancel` / `post` / `validate` — on one or more
  records. **This is not a generic method runner.** The caller names a
  semantic action; a hardcoded `(model, action) -> Odoo method` map
  resolves it. The caller can never supply a method name. Supported
  pairs:

  | Model | Actions |
  |---|---|
  | `purchase.order` | confirm (`button_confirm`), cancel (`button_cancel`) |
  | `sale.order` | confirm (`action_confirm`), cancel (`action_cancel`) |
  | `account.move` | post (`action_post`), cancel (`button_cancel`) |
  | `stock.picking` | validate (`button_validate`), cancel (`action_cancel`) |

  Any pair not in the map is refused. The map lives in
  `src/odoo_mcp/security/document_actions.py` and is non-config-
  overridable — the same shape as `MODEL_DENYLIST` and
  `odoo_archive_or_delete`'s mode choice.

  Full prod-guard pipeline: unlock + dry-run + confirmation token +
  audit, identical to `odoo_write`. The dry-run preview reads and
  shows each record's current `state` (only `id` + `state` — no
  sensitive data, no redaction needed).

  When an Odoo method returns a wizard dict instead of completing
  (notably `stock.picking.button_validate` asking about a backorder),
  the response carries `committed: false` and
  `needs_manual_completion: true` rather than claiming success.

- **`Operation.DOCUMENT_ACTION`** added to the closed operation enum
  and to `_WRITE_OPS`. **`OdooClient.call_document_action`** — a
  named, map-constrained method wrapper, the same controlled shape as
  `message_post`. Not a generic `execute_kw`.

### Security

- This release expands the write surface (a new write-class
  operation). It does NOT add a generic `execute_method`, does NOT
  let the caller pass an Odoo method name, and does NOT widen the
  model surface — `odoo_run_document_action` works only on the four
  models in the hardcoded action map, and each must still pass the
  per-instance allowlist. Reset-to-draft (`button_draft` /
  `action_draft`) is deliberately excluded: un-posting an invoice or
  reverting a confirmed order has accounting / legal implications and
  would get its own review.

  **Why this matters operationally:** using `odoo_write` to set a
  document's `state` field directly (e.g. `state="purchase"`) was
  always technically allowed but is wrong — it skips Odoo's workflow
  logic, so no pickings / stock moves / downstream automation fire.
  `odoo_run_document_action` is the correct path and the tool
  description tells the model so.

### Changed

- **ROADMAP.md gains a full "OAuth authentication to Odoo" section.**
  Documents why OAuth-to-Odoo is not available today (XML-RPC has no
  bearer slot; `auth_oauth` yields a web-session not an API token;
  Odoo-as-OAuth2-provider + JSON-2 needs custom modules unavailable on
  Odoo Online), what a real optional OAuth backend would look like
  architecturally, and the three concrete prerequisites that would
  unblock building it. No code change — a tracked request, not
  in-progress work.

## [0.16.4] - 2026-05-12

Lower the first-time setup friction without weakening the credential
model. No new write surface, no new env var, no guardrail change.

### Added

- **Setup wizard can generate the API key for you.** `odoo-mcp setup`
  (and `--add`) now asks how to authenticate:

    1. Paste an API key you created yourself in the Odoo UI (the
       previous behaviour, still required for 2FA accounts).
    2. Type your Odoo password once — the wizard authenticates,
       generates the key via `res.users.apikeys._generate`, stores it
       in the OS credential store, and discards the password. **No
       Odoo UI navigation.**

  Option 2 is the recommended path for accounts without 2FA. If
  generation fails for any reason (wrong password, 2FA enabled,
  network), the wizard falls back to manual paste rather than aborting.

  The password is used for exactly two XML-RPC calls (authenticate +
  generate) and never stored. This is the same one-shot pattern as
  `odoo-mcp renew-key` — the key-generation logic is now a single
  shared helper (`_generate_api_key_via_password`) used by both.

### Changed

- `odoo-mcp renew-key` internals refactored onto the shared
  `_generate_api_key_via_password` helper. Behaviour unchanged; the
  duplicated authenticate-then-generate block is gone.

- `GETTING_STARTED.md` section 3 rewritten: the manual "create a key
  in the Odoo UI" walkthrough is now Option 1, with the new
  wizard-generates-it path as Option 2 (recommended). 2FA users are
  told up front they need Option 1.

### Not done — and why

- **No OAuth against Odoo.** Odoo's external API (XML-RPC) does not
  accept OAuth tokens; `auth_oauth` is web-login SSO only. There is no
  authorization-code flow on the API layer. The wizard's
  password-once-then-generate flow is the closest achievable
  low-friction UX. Revisited only if a future Odoo JSON-2 API adds
  bearer-token support — see [ROADMAP.md](ROADMAP.md).
- **No persistent password storage.** The password is never written
  to the credential store; only the generated API key is. Storing the
  password would be a strictly worse credential than the 1-day key it
  would replace.

## [0.16.3] - 2026-05-12

Repo maturity pass driven by an external review. Targeted security,
adoption, and supply-chain hardening — no code path changes, no
guardrail changes. Same dispatcher, same denylist, same tests.

### Security

- **Release attestation is now hard-fail.** The
  ``actions/attest-build-provenance`` step in ``release.yml`` no
  longer carries ``continue-on-error: true`` — a failed Sigstore
  attestation now blocks the release. The repo is public so this
  works; if you flip the repo private again, attestations will fail
  and that is intentional. Recovery for transient failures: delete
  the tag, push again, the workflow re-runs cleanly.

- **CI / release workflows pinned to full commit SHAs**, with the
  resolved tag in a trailing comment. Closes the supply-chain hole
  where a compromised action repo could update a tag to point at a
  malicious commit. Bump via dependabot or manually re-resolve via
  ``gh api repos/<org>/<repo>/git/refs/tags/<tag> --jq .object.sha``.

- **``pip-audit`` advisory job in CI.** Runs against the runtime
  dependency tree on every push / PR. Currently
  ``continue-on-error: true`` — first introduction, no vuln-response
  SLA yet. Promote to hard-fail when the policy lands (see
  ``ROADMAP.md``).

- **Package metadata verification in CI** — asserts ``pyproject.toml``
  is internally consistent (name, version present, license MIT,
  Python floor, required deps). Prevents a malformed metadata
  release.

### Added

- **[VERIFY.md](VERIFY.md)** — verified-release install flow. Download
  the release artefact, verify the Sigstore attestation, verify
  ``sha256``, then install. **The README now recommends this for
  production**; the ``curl | bash`` one-liner is explicitly labelled
  as a convenience path.

- **[RELEASE.md](RELEASE.md)** — release checklist with security
  review gates, tag-and-push commands, post-release verification,
  and an explicit "what to do if release workflow fails" section.

- **[ROADMAP.md](ROADMAP.md)** — open owner decisions surfaced as a
  document so external feedback can land against concrete proposals.
  Covers PyPI ownership, Docker registry, signed tags vs release
  attestations, JSON-2 / Odoo 19+ transport, supported Odoo version
  matrix, real Odoo smoke tests in CI, vulnerability response SLA.
  Also documents what is **deliberately not** planned (multi-tenant,
  generic ``execute_kw``, web UI, ...).

- **README badges** for CI, latest release, license, Python versions,
  and Sigstore build provenance.

- **README compatibility matrix** — explicit which Odoo versions,
  Python versions, OSes, and MCP clients are tested vs. supported
  vs. planned. Includes the "no external security audit" notice and
  links to the threat model.

- **README example output section** — sample output for
  ``odoo-mcp doctor``, ``odoo_help`` (terse), a prod dry-run write
  + confirmation flow, and ``odoo-mcp audit --stats``. Lets a
  first-time reader see what the tool actually returns before
  installing.

- **SECURITY.md threat-model matrix** — compact threat → mitigation
  (with code pointer) → tests → residual risk table for every
  defended threat. Sits above the existing per-threat narrative.

- **SECURITY.md "Safe production setup checklist"** — eleven-item
  checklist mapping straight to the actions and config knobs that
  make a production install safe (dedicated non-admin Odoo user,
  strict ``allowed_models``, ``production = true``, audit-log
  review cadence, verified-release install, ...). The "open
  allowlist = discovery/staging, strict = recommended for
  production" stance is now explicit.

- **GitHub issue templates** — security bug (with private-report
  redirect), Odoo compatibility bug, MCP client integration bug,
  feature request (with security-impact dropdown to keep the bar
  high on broadening the surface).

- **GitHub PR template** with explicit security-impact and docs-impact
  sections, plus a reviewer checklist.

### Notes on what was deliberately NOT taken from comparable projects

External review compared the repo to ``tuanle96/mcp-odoo``. The
following ideas from that survey were intentionally not adopted:

- **Generic ``execute_method`` tool.** Even with an allowlist, the
  surface area is too large to threat-model per call. We keep
  per-method named tools instead.
- **``fields=["*"]`` wildcard reads.** Caller must pass an explicit
  list or rely on the curated ``smart_fields`` default; both go
  through redaction.
- **HTTP / SSE transport.** stdio-only is a deliberate scope
  decision tied to the per-user credential model. See SECURITY.md
  scope section.
- **Auto-update with stored password.** Persistent password storage
  to bypass Odoo's API-key expiry would defeat the policy that
  expiry is designed to enforce. We ship ``odoo-mcp renew-key`` as
  the one-shot daily flow instead.
- **Shared service account pattern.** One Odoo user for many human
  consultants kills per-user audit and ACL scoping.

These are all documented in [ROADMAP.md "What is NOT planned"](ROADMAP.md).

## [0.16.2] - 2026-05-12

Real-world fix for deltix's own Odoo Online deployment: non-admin API
keys expire after 1 day by platform policy, and Odoo Online forbids
custom modules — so v0.16.1's addon approach doesn't apply to Online
deployments. The daily-renewal flow is the only honest fix that fits
inside Odoo Online's constraints.

### Added

- **``odoo-mcp renew-key INSTANCE`** CLI command. One-shot daily
  renewal flow:

    1. Reads instance config + username from keychain.
    2. Prompts for the user's Odoo password (not echoed, not stored).
    3. Authenticates via password to Odoo's stock XML-RPC.
    4. Calls ``res.users.apikeys._generate`` on the user's own
       account to produce a fresh key.
    5. Stores the new key in the OS credential store, overwriting
       the previous one.
    6. Drops the password reference immediately after use.

  Works for any user without 2FA enabled (2FA blocks password-auth
  via XML-RPC; those users must generate keys manually in Odoo).
  Surfaces clear errors for wrong password, missing username,
  unknown instance, and Odoo refusing the generate call.

  Workflow for daily users on Odoo Online:

  ```bash
  # Each morning, takes ~30 seconds
  odoo-mcp renew-key prod
  # → Type Odoo password
  # → Key refreshed, valid for the next 24h
  ```

- Nine new tests in ``test_renew_key.py`` covering happy path, wrong
  password, missing username, unknown instance, empty password,
  generate-side fault, the ``__main__`` dispatch, and the
  ``Usage`` error for missing arg.

### Why this is here

The session-long context: ``v0.16.1`` shipped a small Odoo addon that
overrides the 1-day API key default — but that addon can only be
installed on self-hosted Odoo or Odoo.sh. **Odoo Online forbids
custom modules**, so the addon is dead-in-the-water for SaaS Odoo
customers. The 1-day expiry is enforced at the Odoo platform layer
and cannot be relaxed from within the database.

This release adds the only fix that fits inside Online's constraints:
**accept the 1-day limit and provide a clean way to renew daily**.
30 seconds of friction per user per day, no stored password, no
shared service account.

## [0.16.1] - 2026-05-12

Documentation + a new optional Odoo-side addon. No MCP code change.

### Added

- **``odoo_addon/odoo_mcp_long_lived_keys/``** — small Odoo addon
  (~50 lines of Python) that adds a per-user **"Allow long-lived MCP
  API keys"** checkbox to the user form. When ticked by an admin,
  that user's next API key gets a 90-day expiry instead of the 1-day
  default that some Odoo installs enforce for non-admin users
  (Odoo Online, `auth_password_policy`, custom modules).

  Real-world driver: the v0.15.x advice to just "add users to a
  group" did not actually change anything in Odoo, because groups
  are inert labels until code references them. This addon is that
  code. An admin ticks the checkbox per user, the user generates
  a new API key, and the override fires.

  See [`odoo_addon/odoo_mcp_long_lived_keys/README.md`](odoo_addon/odoo_mcp_long_lived_keys/README.md)
  for install / use / customise / revoke. Status: **tested locally
  only**. Install on a non-prod Odoo first.

- **``odoo_addon/README.md`** index page enumerating both addons
  (long-lived keys + companion) with a one-line summary and status
  per addon.

## [0.16.0] - 2026-05-09

This release reverses v0.13.1's blanket refusal of outbound
communications, but only behind two independent opt-ins and the full
existing prod-guard pipeline. Default behaviour is unchanged — out of
the box the MCP still cannot email anyone.

### Added

- **``odoo_send_message`` tool.** Wraps Odoo's ``message_post`` to
  send a chatter message + email (``message_type="comment"``) or post
  an internal log note (``message_type="notification"``) on a record
  of any allowlisted model. The full pipeline:

  1. ``ODOO_MCP_ENABLE_EXTERNAL_COMMS=1`` in the process environment.
  2. ``external_comms_enabled = true`` on the target instance in
     ``config.toml``.
  3. ``odoo_enable_prod_writes`` unlock on production instances.
  4. Dry-run first — *on prod AND dev*. The default for sends is
     ``dry_run=true`` regardless of instance, because an accidentally
     sent email is equally costly in any environment.
  5. Confirmation token bound to (instance, op, model, unlock window).
  6. Burst budget shares ``max_commits_per_unlock`` with other writes.

  The dry-run preview includes the verbatim body, subject, and recipient
  partner_ids so a human can see exactly what will go out.

  ``subtype_xmlid`` is forced from the ``message_type`` value
  (``mail.mt_comment`` ↔ ``mail.mt_note``) so a caller cannot post a
  "log note" that actually triggers an email blast via a mismatched
  subtype.

- **``Operation.SEND_MESSAGE``** added to the closed operation enum
  and to ``_WRITE_OPS``. The dispatcher's ``check_write`` /
  ``effective_dry_run`` / ``consume_pending`` chain applies unchanged.

- **``OdooClient.message_post()``** — the one and only method-execute
  primitive on the client. Calls ``model.message_post(...)`` for a
  specific record id; rejects any ``message_type`` outside
  ``{"comment", "notification"}``.

- **``InstanceConfig.external_comms_enabled`` (default ``False``)** —
  per-instance opt-in. Without it, the dispatcher refuses
  ``odoo_send_message`` with a clear error pointing at the config
  knob.

- **Tool advertisement gating.** ``odoo_send_message`` is filtered out
  of ``tools/list`` unless the env var is set AND at least one
  configured instance has ``external_comms_enabled``. A
  well-behaved client doesn't see the tool until both gates are open.

### Tests

- 12 new tests in ``test_send_message.py`` covering: env-var gate,
  per-instance gate, dev defaults to dry-run, dev commit requires
  token, prod commit requires unlock + token, message_type validation,
  partner_ids type-check, ``would_send_email`` flag, advertisement
  gating, and ``ODOO_MCP_READ_ONLY`` still wins.

- ``test_disabled_tools.py`` updated to expect ``odoo_send_message``
  hidden from the default tool list.

### Deliberately not in this release

- **WhatsApp send** — depends on Odoo Enterprise + the WhatsApp
  module. Framework is in place (``Operation.SEND_MESSAGE`` is generic
  enough); a follow-up release will add ``odoo_send_whatsapp`` once
  the Enterprise schema is pinned.

- **Separate burst budget for sends.** Currently shares
  ``max_commits_per_unlock``. Argument for a separate
  ``max_messages_per_unlock``: a chatty AI could exhaust the write
  budget on emails. Defer until real usage shows it matters.

## [0.15.11] - 2026-05-09

### Security

- **Denylist expanded to close three rights-modification gaps.** Four
  additional models added to ``MODEL_DENYLIST`` so the MCP cannot be
  used (read or write) to grant or revoke privileges, even on
  instances where Odoo's per-user ACLs would technically allow it:

  - ``res.users.role`` and ``res.users.role.line`` — Odoo Enterprise
    role-based access. Writing here assigns rights in the same way
    ``res.users.groups_id`` does on Community. Already covered for
    Community via the existing ``res.users`` / ``res.groups`` entries;
    this closes the Enterprise variant.

  - ``base.automation`` (and its ``.lint`` / ``.line.test`` siblings)
    — automated actions that can run Python or modify other records
    under sudo. Same threat class as ``ir.actions.server``: a write
    here is rights modification by proxy.

  - ``mcp.access.profile`` — the model added by the optional
    companion addon (``odoo_addon/odoo_mcp_companion/``) that controls
    who can act through the MCP. Defense in depth: even when that
    addon is installed and exposed, the MCP itself must never let a
    caller reconfigure its own gate.

  No code path change; this is purely an extension of the existing
  hardcoded denylist. The "Auth / user / group" and "Stored
  executable content" sections of the denylist now carry comments
  explaining that the intent is "no rights modification via MCP" so a
  future reviewer adding a new entry knows the category.

### Tests

- New ``test_rights_modification_models_all_denied`` test pins every
  known privilege-escalation vector to a specific denylist entry
  grouped by escalation type. If a future refactor accidentally drops
  one, this test fails before merge.

- ``test_denylist_contents_are_locked_in`` updated to include the new
  entries.

## [0.15.10] - 2026-05-09

Documentation fixes. No code change.

### Changed

- **README ``scan-custom`` claim no longer promises Odoo 18.0.** The
  embedded reference schema is currently Odoo 18 Community, but the
  visitor may be running Odoo 16, 17, or 19. Rewrote the bullet to
  state the reference version explicitly and note that the diff still
  works on other versions (with a small custom-field overcount on
  newer Odoo, harmless).

- **Security-reporting email corrected to ``hello@deltix.pro``** with
  subject prefix ``[odoo-mcp security]``. The old
  ``security@deltix.pro`` did not exist. Updated in README,
  SECURITY.md, and GETTING_STARTED.md.

## [0.15.9] - 2026-05-09

Documentation-only release. No code change.

### Added

- **``GETTING_STARTED.md``** — standalone first-run guide for someone
  landing on the GitHub repo without prior context. Covers:
  prerequisites; how to pick the right Odoo user (and why not admin);
  step-by-step API-key creation on Odoo 16 / 17 / 18 (including the
  re-authentication prompt, the "shown once" copy step, the "no API
  Keys tab" troubleshooting, and the fact that *any* internal user
  can create their own — no special permission needed); minimum
  groups by use case; install commands per platform; verify;
  first-test prompts; a ten-row gotcha table; rotation / revocation.

  About 5 min to read, 10 min to install through.

### Changed

- **README Quick Start** now leads with a clearly visible banner
  pointing first-time installers at ``GETTING_STARTED.md``. Short
  three-step recap stays for people who already know what they're
  doing. Windows install command added inline.

## [0.15.8] - 2026-05-09

Documentation correction. No code change.

### Changed

- **Scope clarification: one MCP install = one Odoo deployment.**
  README and SECURITY.md now state explicitly that the multi-instance
  feature is for dev / staging / prod of the *same* Odoo deployment —
  not for pointing one MCP install at multiple unrelated organisations'
  databases. The single audit log, shared OS keychain entries, and
  global allowlist / denylist / sensitive-field policy all assume
  single-organisation scope. For consulting work that touches multiple
  unrelated customer Odoos, the right pattern is one MCP install per
  customer on separate OS user accounts. The architecture was already
  single-org by design; v0.15.7's marketing copy implied otherwise,
  and this release corrects that.

- **SECURITY.md** gains a "Scope and shared responsibility" section
  enumerating the four things that are per-process and therefore
  shared across configured instances (audit log file, OS credential
  store entries, fields cache, allowlist / denylist / sensitive-field
  policy).

- **README** ``What you get`` and ``Where it fits`` rewritten:
  "multi-instance, multi-client, multi-user" → "Dev + prod side by
  side". "Consulting on multiple customer Odoos" → "In-house operators
  with one Odoo deployment". One-line scope banner at the top of the
  README.

- **Corporate link normalised** to ``https://www.deltix.pro`` in the
  README.

## [0.15.7] - 2026-05-09

### Changed

- **README leads with value, not implementation.** New top sections:
  one-line hook, "What you get" with six concrete benefits, "Where it
  fits" with four use cases. Technical detail unchanged below the
  fold; the existing tool / CLI / configuration reference reads the
  same. No code change.

## [0.15.6] - 2026-05-09

### Added

- **``odoo-mcp setup --acknowledge-admin NAME``** — standalone command
  to persist ``refuse_admin_on_production = false`` for an instance
  whose config was already written under an older version. Closes the
  loop for users stuck in the v0.15.5 wizard improvement: that release
  fixed fresh-install flow, this one repairs an already-broken
  config without requiring ``--remove`` + ``--add`` (and the lost
  Keychain entry that goes with it).

  Same explanation + confirmation prompt as the wizard branch.
  Idempotent. ``odoo-mcp doctor`` afterwards verifies the instance
  authenticates.

## [0.15.5] - 2026-05-09

### Fixed

- **Setup wizard now handles admin-on-prod gracefully.** Real-world
  feedback from a colleague: their Odoo API key has admin rights (the
  only credential available on many SMB Odoo SaaS deployments), and
  ``odoo-mcp setup`` happily wrote the config — then doctor failed
  with a ``Refusing to use admin credentials`` error and no obvious
  next step. The wizard would silently abort.

  The wizard now intercepts the same condition before doctor runs and
  prompts the user with a clear explanation of the security trade-off
  plus two paths:

  1. **Recommended:** abort, create a non-admin Odoo user with the
     groups the role actually needs, rerun ``odoo-mcp setup``.
  2. **Acknowledge the risk:** type ``acknowledge`` to proceed. The
     wizard writes ``refuse_admin_on_production = false`` to the
     instance's TOML block. The MCP's denylist, redaction, and
     prod-write guard still apply; only Odoo's per-user record-rule
     scoping is bypassed.

  The check is skipped when the instance is non-production, when it's
  authenticated as a non-admin, or when the opt-out is already
  persisted from a previous run.

  No security-posture change: the underlying refusal behaviour is
  unchanged, the wizard just exposes the choice instead of leaving
  the user stuck.

## [0.15.4] - 2026-05-08

Performance + code-quality pass. No behaviour change visible to MCP
clients; same tools, same prompts, same security envelope.

### Changed

- **Audit log loading is now date-window aware.** ``_load_all_entries``
  accepts ``since_minutes`` and skips rotated ``audit-YYYY-MM-DD.jsonl``
  files dated before the cutoff. ``audit --since N`` and ``audit --errors``
  now only open the files that could plausibly contain matching entries
  instead of reading the entire 30-day rotation history. Real win on
  installs that have been running for weeks. ``--stats`` over the full
  history is unchanged (still loads everything by design).

- **``odoo-mcp status`` only loads the last 24 hours of audit log.**
  The "last call X ago" line and the "recent activity" tail only need
  recent data; loading 30 days for five lines of output was waste.
  Per-instance "last call" lines may now show ``no activity`` instead
  of ``Xd ago`` for instances idle longer than 24h.

- **DRY the field-resolution branch in ``_search_read`` / ``_read``.**
  The 30-line "fields = override-or-smart-or-explicit" block was
  duplicated. Now in ``Dispatcher._resolve_read_fields``. ~60 fewer
  lines of dispatcher code, and any future tweak (e.g. read-specific
  default) lands in one place.

### Tests

- ``test_audit_files_excludes_old_rotations_when_since_set`` pins the
  date-filtering behaviour against accidental regression.



Audit-pass release. No new features — quality / cleanup / docs only.

### Fixed

- **`next_offset` on `odoo_search_read` now anchors on the actual
  record count, not the requested limit.** If Odoo over-delivers
  (third-party module returning more than ``limit`` rows), the buggy
  formula ``offset + limit`` would have skipped records on the next
  page. New regression test pins the behaviour.

### Changed

- **Better integration test for ``ODOO_MCP_DISABLE_TOOLS``.** Previous
  test re-ran the filter logic in test code (the comment admitted as
  much). Now it pulls the registered ``ListToolsRequest`` handler out
  of the live MCP server and asserts on the actual advertised tool
  list. Two new tests cover the no-env-var and unknown-name cases.

- **README, SECURITY.md, and ONBOARDING.md document the runtime-scoping
  env vars.** ``ODOO_MCP_READ_ONLY``, ``ODOO_MCP_DISABLE_TOOLS``, and
  ``ODOO_MCP_TOOL_LATENCY_BUDGET_MS`` now appear in all three places
  with what they do, when to use them, and the doctor surface.
  ONBOARDING also gains a "Day-to-day commands" row for ``odoo-mcp
  client-config`` and the four ``--json`` modes.

- **Shared test fixtures in ``tests/conftest.py``** (``make_instance_config``
  and ``make_app``) replace the boilerplate that was duplicated across
  ~8 test files. ``test_disabled_tools.py`` migrated as the first
  consumer; the rest will move opportunistically.

## [0.15.2] - 2026-05-07

### Added

- **``ODOO_MCP_TOOL_LATENCY_BUDGET_MS`` env var.** Set to a positive
  integer to make the dispatcher emit a ``WARNING`` log line tagged
  ``slow_tool_call`` whenever a successful tool call exceeds the
  budget. Pure observability — never alters call results. Doctor
  surfaces the configured budget so you can spot it in pre-flight.

- **``--json`` on ``odoo-mcp cache --info`` and ``odoo-mcp status``.**
  Cache emits the same dict ``cache.info()`` returns; status emits a
  new compact payload (``version``, ``config_path``,
  ``audit_log_path``, plus per-instance ``uid`` / ``rate_limit`` /
  ``writes_unlocked`` / ``commits_remaining``). All four CLIs that
  matter for CI ingestion — ``doctor``, ``audit``, ``cache``,
  ``status`` — now have a stable JSON mode.

- **``odoo_my_changes_today`` prompt.** End-of-day recap workflow:
  queries records with ``write_uid = <current uid>`` and
  ``write_date >= today midnight`` across the common business models.
  Read-only.

- **Optional Odoo-side companion addon (skeleton).** ``odoo_addon/``
  ships a minimal Odoo module (``odoo_mcp_companion``) that adds two
  security groups (MCP Read Only / MCP Read+Write) and a
  ``mcp.access.profile`` model so an Odoo admin can centralize
  server-side scoping for whichever user the MCP authenticates as.
  **Marked as untested by maintainers** — see ``odoo_addon/README.md``.
  Treat it as a starting point for defense-in-depth, not a drop-in
  module.

### Changed

- ``odoo-mcp doctor`` now surfaces ``ODOO_MCP_DISABLE_TOOLS`` and
  ``ODOO_MCP_TOOL_LATENCY_BUDGET_MS`` alongside the existing
  ``ODOO_MCP_READ_ONLY`` warning, so all three observability /
  scoping env vars are visible in pre-flight.

## [0.15.1] - 2026-05-07

### Added

- **``ODOO_MCP_DISABLE_TOOLS`` env var.** Comma-separated list of tool
  names that are filtered out of the ``tools/list`` advertisement so a
  well-behaved MCP client never sees them. Complements
  ``ODOO_MCP_READ_ONLY``: where read-only refuses every write call,
  this hides specific tools entirely. Useful for handing a junior
  consultant access to ``odoo_search_read`` / ``odoo_read`` without
  also exposing ``odoo_archive_or_delete``. Unknown names are logged
  and ignored — they don't fail the server.

- **``has_more`` flag on ``odoo_search_read`` responses.** When the
  returned page is at the limit, the response now includes
  ``has_more: true`` plus ``next_offset`` so Claude knows to paginate
  or narrow the domain. Costs no extra round trip — purely derived
  from existing data. When fewer records than the limit come back,
  ``has_more`` is ``false`` and ``next_offset`` is omitted.

- **``--json`` output on ``odoo-mcp doctor`` and ``odoo-mcp audit``.**
  Doctor emits ``{"ok": bool, "steps": [...], "warnings": [...]}``,
  ready for CI gating. Audit emits the filtered entries (or, with
  ``--stats``, a per-tool array of latency / count rows). Both modes
  suppress the human-formatted output so the JSON line is the only
  thing on stdout.

## [0.15.0] - 2026-05-07

This release widens the MCP's audience beyond Claude Desktop / Claude
Code by shipping ready-to-paste config snippets for every popular MCP
client, and fleshes out the prompts library with industry-tied
workflows aimed at the deltix consulting base.

### Added

- **``odoo-mcp client-config`` CLI.** Prints config snippets for the
  popular MCP clients with the absolute ``odoo-mcp`` binary path
  pre-resolved (so consultants don't have to figure out where ``uv
  tool install`` placed it). Supported: Claude Desktop, Claude Code,
  OpenAI Codex CLI, Cursor, Windsurf, Continue.dev, Zed, plus a
  generic stdio fallback for any MCP-compliant client. Modes:
  ``--list`` enumerates supported clients; ``--client NAME`` prints
  one block; ``--detect`` scans the local machine and prints blocks
  for clients whose config dirs exist; no flag prints all blocks. The
  command is informational — it never writes any client config files
  itself (``odoo-mcp setup`` still does the Claude Desktop / Codex
  registration).

- **Six industry-tied prompts.** Round out the prompts library with
  workflows mapped to our four industry templates:
  ``odoo_low_stock_check`` (wholesale), ``odoo_open_manufacturing_orders``
  (manufacturing), ``odoo_hr_leave_overview`` (HR),
  ``odoo_timesheet_review`` (professional services),
  ``odoo_unposted_journal_entries`` (accounting), and
  ``odoo_top_revenue_customers`` (cross-industry sales review). Each
  prompt accepts an ``instance`` argument plus optional scoping
  parameters (``warehouse_id``, ``department_id``, ``weeks_back``, ...).

## [0.14.2] - 2026-05-07

### Added

- **L2 fields cache schema versioning.** The persistent SQLite cache
  payload now embeds a ``_v`` marker; rows written by older versions
  are silently treated as a miss and re-fetched. This was the missing
  piece for v0.14.1's ``store`` attribute — without it deployed
  installs would have used stale cache entries (without ``store``) for
  up to 24 hours, defeating the new computed-field skip in smart
  selection.

- **Per-model ``smart_fields_overrides`` in instance config.** A new
  ``[instances.NAME.smart_fields_overrides]`` table maps a model to an
  ordered list of fields. When the caller omits ``fields`` on
  ``odoo_search_read`` / ``odoo_read``, the override replaces smart
  selection for that model. Sensitive-field redaction still applies on
  the response — overrides cannot leak passwords / VAT / etc. Lets
  consultants tune what a klant's ``account.move`` returns by default
  without touching code.

- **``odoo-mcp audit --stats``.** Per-tool call counts, ok/error split,
  and p50/p95/max latency in ms, sorted by busiest tool. Helps
  operators spot slow models, runaway loops, or unhealthy instances.
  Combines with ``--instance`` and ``--since`` filters; ignores
  ``--tail`` because percentiles need the full sample.

- **Doctor surfaces ``ODOO_MCP_READ_ONLY``.** When the env var is set,
  ``odoo-mcp doctor`` emits a warning line so a consultant who flipped
  the gate for a demo doesn't later wonder why every write fails.

## [0.14.1] - 2026-05-07

### Added

- **Read-only session toggle.** Setting the env var
  ``ODOO_MCP_READ_ONLY=1`` (or ``true`` / ``yes`` / ``on``) refuses
  every write-path tool — ``odoo_create``, ``odoo_write``,
  ``odoo_archive_or_delete``, ``odoo_enable_prod_writes`` —
  irrespective of per-instance ``production`` flags or unlock state.
  Reads remain unaffected. ``odoo_list_instances`` surfaces
  ``session_read_only: true`` so Claude knows the gate is on. Useful
  for demos, training sessions, and external consultants.

- **Studio / custom field markers in ``odoo_describe_model``.** Fields
  whose name starts with ``x_`` get ``_custom: true``; fields whose
  name starts with ``x_studio_`` get both ``_custom: true`` and
  ``_studio: true``. Lets Claude tell client-specific custom fields
  apart from standard Odoo fields without an extra ``scan-custom``
  invocation. Audit log records ``custom_field_count`` /
  ``studio_field_count`` per call.

- **Smart-field selection now skips non-stored computed fields.** The
  client's ``fields_get`` call requests the ``store`` attribute, and
  ``select_smart_fields`` excludes any field where ``store=False``.
  Cuts further response noise on models with many computed
  display-only fields. Falls back conservatively (keep the field) when
  ``store`` is missing — older L2-cache entries still work.

- ``OdooClient.username`` public property — diagnostic-friendly
  alternative to the previous private ``_get_credentials()`` access in
  the dispatcher. Returns ``None`` if credentials haven't been loaded
  yet.

## [0.14.0] - 2026-05-07

This release closes the most visible feature gaps with the other Odoo MCP
projects on GitHub (notably tuanle96/mcp-odoo and hachecito/odoo-mcp-improved)
without compromising our security position. Three additions:

### Added

- **`odoo_diagnose_access` tool.** Reports `read` / `write` / `create` /
  `unlink` rights for the authenticated Odoo user on a given model, plus
  the user's `uid`, `login`, and admin status. Read-only and goes through
  the same allowlist + rate-limit pipeline as every other tool. Useful
  when a `search_read` returns fewer records than expected, or before
  attempting a write to a model the API user may not have rights on.
  Backed by Odoo's `check_access_rights(op, raise_exception=False)`.

- **Smart-default fields on `odoo_search_read` and `odoo_read`.** The
  `fields` argument is now optional. When omitted, the dispatcher
  computes a curated default: priority columns (`id`, `name`,
  `display_name`, `state`, `partner_id`, ...) followed by an
  alphabetical fill, capped at 25 fields. Binary, HTML, one2many,
  many2many, audit fields (`create_uid`, `__last_update`,
  `message_*`, `activity_*`), and any sensitive field (always-redacted
  or default-hidden) are excluded. Sensitive-field policy still applies
  in full — smart selection never bypasses redaction. Responses include
  a `smart_fields_used` array so the caller can see what was selected.
  Explicit `fields=[...]` still works unchanged.

- **MCP prompts library.** Six pre-canned prompts surface in clients
  like Claude Desktop as slash-commands: `odoo_month_end_check`,
  `odoo_overdue_invoices`, `odoo_find_duplicate_partners`,
  `odoo_pipeline_review`, `odoo_recent_changes`,
  `odoo_diagnose_permissions`. Each prompt requires an `instance`
  argument and emits a short instruction message that nudges Claude
  into running the right sequence of existing tools. Prompts never
  call Odoo themselves — they're just templates.

### Changed

- `odoo_help` tool list now includes `odoo_diagnose_access`.
- `Operation` enum gains `DIAGNOSE_ACCESS` (read-side). No write paths
  added.

## [0.13.2] - 2026-05-06

### Fixed

- **Launcher migration no longer leaves Claude Desktop pointing at a
  deleted file.** `odoo-mcp update`'s migration of the legacy
  `~/.odoo-mcp/launch.sh` wrapper now rewrites the Claude Desktop
  registration BEFORE deleting the script, uses a substring match
  (`"launch.sh" in command`) so symlink-resolved or otherwise
  non-identical paths are still detected as legacy entries, verifies
  the rewrite landed by re-reading the config, and aborts the
  migration (leaving `launch.sh` in place) if any step fails. If a
  legacy script is found with no matching registration, a warning is
  printed and nothing is touched. This fixes a real-world install run
  where the migration deleted `launch.sh` but left Claude Desktop
  pointing at the missing file, producing
  `Failed to spawn process: No such file or directory` on the next
  Claude Desktop restart.
- **Sigstore issuer quirks no longer hard-fail the installer.**
  `verify_release_attestation` and the bash mirror in `scripts/install.sh`
  now treat a wider set of patterns as environmental (soft-fail with a
  prompt) — `sigstore`, `issuer`, `tuf`, `network`, `connection refused`,
  `timeout` — and reserve hard-fail for explicit tampering signals
  (`signature does not match`, `signature mismatch`, `tampered`,
  `invalid signature`, `unexpected signer`, `wrong owner`). Public-good
  Sigstore issues no longer force users to install with
  `--skip-verification`.

### Added

- **Codex registration.** The setup wizard now registers `odoo-mcp` in
  `~/.codex/config.toml` under `[mcp_servers.odoo-mcp]` when Codex is
  installed, using the same direct `odoo-mcp launch` stdio command as
  Claude Desktop. `odoo-mcp update` also refreshes this registration for
  existing installs.

### Changed

- **Uninstall cleans up Codex too.** `odoo-mcp uninstall` now removes the
  Codex MCP registration while preserving unrelated Codex config sections.
- **Legacy macOS Keychain migration.** Launch-time credential loading now
  falls back to the pre-v0.13.0 macOS Keychain layout and writes the value
  into the new cross-platform keyring schema on first successful read, so
  older users do not need to re-enter API keys after updating.

## [0.13.1] - 2026-05-05

Pilot-blocker pass after Timon's fresh-Mac install run, plus four
operator-facing tweaks. No security model changes; the `mail.message`
read default is loosened but a new hard write-blocklist closes the
side-door it would otherwise open.

### Fixed

- **Installer survives a Mac without Homebrew (B1).** `install.sh` now
  detects missing `brew` *and* missing `gh`, prompts before running the
  official Homebrew installer, sources the right `brew shellenv` for
  Apple Silicon vs. Intel, and falls back to a clear "install Homebrew
  from https://brew.sh first" message on refusal or non-interactive
  shells.
- **Attestation verification is more lenient against environmental
  failures (B2).** `gh attestation verify` failures whose output matches
  any of `no.*attestation`, `404`, `not found`, or `failed to fetch`
  (case-insensitive) now soft-fail with a y/N prompt instead of
  hard-failing. A real signature mismatch (exit 1, none of those
  patterns) still aborts. Same lenience applied to `install.ps1`.
- **`odoo-mcp` resolvable in the same shell after install (B3).**
  `install.sh` now exports `~/.local/bin` to `$PATH` for the running
  process AND appends the export line to `~/.zshrc` / `~/.bashrc` (per
  `$SHELL`) — only if not already present. `install.ps1` mirrors this:
  prepends `%USERPROFILE%\.local\bin` to the current `$env:Path` and
  persists it on the User PATH idempotently.
- **`odoo-mcp doctor` finds Keychain credentials (B4).**
  `run_doctor` now calls `setup_wizard.load_credentials_into_os()` at
  the top of the run (after config load, before per-instance checks).
  Credstore failures are caught and surfaced as a `!` warning rather
  than aborting doctor — the per-instance "missing env var" check is
  still the loud signal if creds are genuinely absent.

### Changed

- **`mail.message` body / subject / author / email fields are readable
  by default (F1).** Removed from `_DEFAULT_HIDDEN["mail.message"]`.
  The cross-model side-door concern is now addressed by a separate hard
  invariant: a new `MODEL_WRITE_BLOCKLIST` in
  `odoo_mcp.security.allowlist` covering `mail.message`, `mail.followers`,
  `mail.notification`. Every write-path handler (`_create`, `_write`,
  `_archive_or_delete`) refuses these models BEFORE prod-guard runs, so
  even an unlocked prod-write window cannot be used to send messages,
  post log notes, or impersonate authors via the MCP. Like
  `MODEL_DENYLIST`, the new blocklist is non-overrideable from config.
  Existing callers that pass `allow_sensitive_fields=["body"]` continue
  to work — the argument is simply a no-op for these now-readable fields.
- **Error hints no longer coach toward workarounds (F2).**
  `ModelNotAllowedError.hint` becomes "Contact your MCP administrator
  if this model should be available." `ProdGuardError.hint` becomes
  "Production writes require explicit unlock by the operator."
  Other hints (capability enumeration, naming discovery, troubleshooting)
  are unchanged because they don't suggest workarounds.
- **API key rotation reminder (F3).** `_credstore.set_secret` now
  writes a sibling timestamp entry (under `odoo-mcp/{instance}/_meta`,
  ISO-8601 UTC) for every tracked secret (currently anything ending
  in `_API_KEY`). `odoo-mcp doctor` reads that timestamp and emits a
  `!` warning per instance whose key is older than
  `rotation_warning_days` (new `[defaults]` key, default 90, range
  0-3650). Instances with no recorded timestamp get a one-line nudge
  to rotate-once and start tracking. `setup --rotate-key NAME` records
  a fresh timestamp automatically. `SECURITY.md` user-responsibilities
  section expanded with the rotation policy.

## [0.13.0] - 2026-05-05

Portability + safety pass before public publication.

### Added

- **Cross-platform credential storage.** New `src/odoo_mcp/_credstore.py`
  wraps the standard `keyring` package; macOS Keychain, Windows Credential
  Manager, and Linux libsecret are now all supported transparently.
  `keyring>=25.0` added as a runtime dependency.
- **Platform-aware Claude Desktop config path.** New
  `_claude_desktop_config_path()` resolves the correct location on macOS
  (`~/Library/Application Support/Claude/...`), Windows
  (`%APPDATA%\Claude\...`), and Linux (`~/.config/Claude/...` per XDG).
- **Windows installer.** New `scripts/install.ps1` mirrors `install.sh`
  step-for-step with PowerShell idiom (winget for `gh` and `uv`,
  `gh attestation verify` with the same soft-fail policy, `--SkipVerification`
  flag).
- **`LICENSE`** at repo root with MIT terms.
- **`SECURITY.md` user-responsibilities section** listing what the
  operator still owns (Odoo ACL configuration, suggestions review, key
  rotation, host hygiene). New "Limitations of liability" section
  pointing at the LICENSE disclaimer.
- **README disclaimer block** at top of file ("As-is software, no
  external audit, supported on macOS / Windows 10+ / Linux with libsecret").
- **README "Not your responsibility either" line** linking to the new
  user-responsibilities section.

### Changed

- **License switched from "Proprietary" to MIT.** README license section
  updated to a single line pointing at `LICENSE`.
- **Claude Desktop registration** now invokes the `odoo-mcp` CLI directly
  (`command: <abs path to odoo-mcp>, args: ["launch"]`) instead of going
  through a `~/.odoo-mcp/launch.sh` shell wrapper. The wrapper was a
  macOS-only intermediate that loaded Keychain creds via `security(1)`;
  with cross-platform credential storage it no longer earns its keep.
  `setup_wizard.load_credentials_into_os()` (the rename of
  `load_launch_env_into_os`; the old name is kept as an alias for one
  release) loads creds in-process at `odoo-mcp launch`.
- **`odoo-mcp update` migration.** When an update detects a legacy
  `~/.odoo-mcp/launch.sh` it rewrites the Claude Desktop registration
  to the direct-CLI form and removes the script. Hand-edited
  registrations pointing elsewhere are left alone.

### Removed

- **`subprocess` calls to `/usr/bin/security`** in `setup_wizard.py`.
  All credential-store access now goes through the keyring wrapper.
- **Default generation of `launch.sh`** in `_cmd_setup`. The legacy
  template (`_write_launch_sh`, accessible via
  `setup --regenerate-launcher`) is kept for one release as a fallback
  for users with old launchers still referenced in Claude Desktop config
  who haven't run `odoo-mcp update` yet.

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
