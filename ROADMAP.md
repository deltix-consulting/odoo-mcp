# Roadmap and open owner decisions

This document is the explicit list of "we know this is a question, we have not yet decided." It exists so feedback can land against a concrete proposal rather than guess.

Anything not on this list is either shipped, deliberately out of scope, or has not yet come up. Open a [feature request](https://github.com/deltix-consulting/odoo-mcp/issues/new?template=feature-request.yml) if you want to add something here.

## Open owner decisions

### PyPI package ownership

**Status:** undecided.

The current model is "verified release from GitHub" — users download a release artefact and `uv tool install` it via the [VERIFY.md](VERIFY.md) flow.

Publishing to PyPI would let people `uvx odoo-mcp` or `pipx install odoo-mcp` directly. Two unresolved items:

- Who owns the PyPI `odoo-mcp` namespace? Squatting it pre-emptively (with a placeholder) is cheap. Leaving it for someone else to grab is a real supply-chain risk.
- Who has the publish token? Today the GitHub Actions release workflow signs with Sigstore — adding `twine upload` would add a long-lived PyPI token to the secret store, with all the rotation / revocation that implies.

**Proposed step:** reserve the `odoo-mcp` PyPI name with a no-op v0.0.0 release that just points at the GitHub repo and the verified-install flow. Decide on automated publishing later, once a vuln-response SLA exists (see "Vulnerability response" below).

### Docker image registry

**Status:** undecided.

A Docker image for isolated local runs is a frequent request. The blockers are not technical:

- Where does it live? `ghcr.io/deltix-consulting/odoo-mcp` is the obvious choice but ties us to GitHub Container Registry. Docker Hub is the more universal address but means a second registry to maintain.
- Credentials handling. The MCP relies on the OS credential store. A Docker container has no Keychain / Credential Manager / libsecret — credentials would have to be mounted at runtime, which is a different security shape than the local install. Documentation needs to be explicit.
- Audit log volume. Default is `~/.odoo-mcp/audit.jsonl` on the host; in a container the audit log must be bound to a host path or it disappears between runs. Default behaviour needs to be safe.

**Proposed step:** ship a `Dockerfile` + `docker-compose.yml` example in a separate `docker/` subdirectory, NOT auto-published as an image. Operators who want it build locally. Promote to a published image when there is concrete demand from more than one user.

### Signed git tags vs release attestations

**Status:** decided — release attestations are the trust anchor, git tag signing is not currently required.

Reasoning:

- Sigstore build provenance attestation already binds the published artefact to the GitHub Actions release workflow run on the specific tag. That covers "did this artefact really come from this repo's CI on this version?"
- Git tag signing adds "is the tag-pusher identity verified?" That has value, but unless we *also* require commit signing (which is more disruptive) we leave a gap between "signed tag at HEAD" and "every commit in the history."
- For a one-maintainer project today, the marginal trust of tag-signing is small. For a multi-maintainer future, this should be revisited together with commit signing.

**What this means for users:** verify the release attestation per [VERIFY.md](VERIFY.md). Do not assume the absence of a signed tag means the release is untrusted; assume the presence of a valid build-provenance attestation means it is.

### JSON-2 / Odoo 19+ transport

**Status:** undecided, research item.

Background: Odoo 20 will drop the legacy JSON-RPC and emphasise the new JSON-2 external API. The current `OdooClient` is XML-RPC over `xmlrpc.client`, which works against Odoo 16 / 17 / 18 (Community + Enterprise + Online) and is expected to keep working on Odoo 19 alongside JSON-2.

The honest position:

- **No urgent need to swap transports.** XML-RPC will be removed at some point but Odoo gives years of notice.
- **JSON-2 as an optional second backend** is the right shape. The `OdooClient` becomes an interface; `_XmlRpcClient` and `_Json2Client` are the two implementations. The dispatcher, security pipeline, audit log, redaction — everything above the client — stays unchanged.
- **Backwards compatibility is on us.** Operators with Odoo 16 keep XML-RPC. Operators on Odoo 19+ get to pick. Default stays XML-RPC until the matrix flips.

**Proposed step:** when Odoo 19 is generally available and stable, add `transport = "xmlrpc" | "json2"` to the per-instance config, implement `_Json2Client`, run the existing test suite against both transports via parameterised fixtures. No urgency before then.

**What is explicitly OUT of scope:** turning `odoo-mcp` into a JSON-RPC HTTP server (the other half of MCP transports). The MCP is stdio-only by design. JSON-2 here means how we talk to Odoo, not how MCP clients talk to us.

### Supported Odoo version matrix

**Status:** decided for now, revisit per Odoo release.

| Odoo version | Status | What changes if we drop it |
|---|---|---|
| 18.0 Community + Enterprise | tested, primary | n/a |
| 17.0 Community + Enterprise | tested, primary | n/a |
| Odoo Online (rolling) | tested, with the daily-key caveat | n/a |
| 16.0 | lightly tested, supported | drop = consultants on legacy installs lose support |
| 19.0+ | not yet | depends on Odoo's release timing |
| 15.0 and earlier | NOT supported | API keys behave differently or do not exist |

**Proposed step:** add a real Docker Compose smoke test (next item) before promoting any version from "lightly tested" to "primary" or vice versa.

### Real Odoo smoke tests in CI

**Status:** not yet — scoped, not implemented.

What this would look like:

- A `tests/integration/docker-compose.yml` that brings up Odoo + Postgres for one disposable version
- A pytest marker `@pytest.mark.integration` on tests that need a live Odoo
- A `make integration` target that brings the stack up, runs the marked tests, brings the stack down
- A CI job that runs these against at least one Odoo version per matrix entry, gated behind a manual workflow dispatch (not on every push — startup is ~60s)

What it gives us: catches "this worked against my dev Odoo but not against stock 17" regressions before users hit them. Especially valuable for `message_post`, `_generate`, and anything that touches Odoo's own ACL.

**Blocker:** the existing test suite mocks XML-RPC. Adding integration tests is not a refactor — it's a new dimension. Worth doing, not yet done.

**Proposed step:** start with a single Odoo 18 Community smoke test that runs `odoo_search_read` and `odoo_describe_model` against a fresh `odoo/odoo:18.0` container. Expand the matrix and tool coverage from there.

### Vulnerability response SLA

**Status:** undefined.

`SECURITY.md` says "expect a response within five business days." That covers acknowledgement, not remediation. For a more mature product we would commit to:

- Critical (denylist bypass, credential leak, prod-write gate bypass): patch within 7 days, advisory within 14
- High: patch within 30 days
- Medium / Low: next regular release

These are not committed today because the maintainer team is small and varies. The `pip-audit` CI job is currently `continue-on-error: true` until this is decided.

**Proposed step:** define the SLA when we have a second maintainer, or when a real vulnerability lands and forces the question.

## Already decided (for reference)

- **stdio MCP transport only.** No HTTP / SSE. See SECURITY.md and the multi-tenant discussion in [the architecture commentary thread](https://github.com/deltix-consulting/odoo-mcp/discussions).
- **No generic `execute_kw` / `execute_method` tool.** Every Odoo method we expose is a named wrapper with its own argument schema. Sales-team request for "make every workflow action callable" → declined; each workflow action gets its own tool.
- **No `fields=["*"]` wildcard.** Explicit fields list required. The `smart_fields` default is the only opt-out, and even there the redaction pipeline still strips sensitive fields.
- **MODEL_DENYLIST is not config-overridable.** Adding a model to the denylist tightens; removing one would require a code change with explicit review.
- **One MCP install = one Odoo deployment.** Multi-tenant SaaS is a different product, not a flag.

## What is NOT planned

- Multi-tenant hosted MCP service — see [SECURITY.md scope section](SECURITY.md#scope-and-shared-responsibility).
- Generic Python-execution / `ir.actions.server` runner.
- Direct browser / web UI (the MCP is for MCP-aware clients).
- Anything that lets a tool call run as a different Odoo user than the one whose API key is in the keychain.
