# Verified release install

For production installs, do **not** use the `curl | bash` one-liner from the README. Use the verified-release flow below: download the release artifact, verify the GitHub / Sigstore build-provenance attestation, verify the SHA-256 yourself, then install.

This is the trust-anchor flow. Every release of `odoo-mcp` is built by the GitHub Actions release workflow on a tag push, the artifact is signed via Sigstore as a [GitHub Build Provenance Attestation](https://docs.github.com/en/actions/security-for-github-actions/using-artifact-attestations), and the same attestation pipeline blocks the release if signing fails.

## Prerequisites

- `gh` CLI ≥ 2.49 with `gh auth login` completed
- `uv` (or `pipx`, or another way to install a Python tool from a local wheel)
- `sha256sum` (Linux/macOS — on Windows use `Get-FileHash`)

## 1. Pick a release

```bash
gh release view --repo deltix-consulting/odoo-mcp
```

Pick the **latest non-prerelease** tag, e.g. `v0.16.2`. **Read the [CHANGELOG.md](CHANGELOG.md) entry first** — security-sensitive behaviour changes (denylist, redaction, prod-write flow, credential handling, audit log) are flagged with a `### Security` heading.

## 2. Download the artifacts

```bash
TAG=v0.16.2
mkdir -p /tmp/odoo-mcp-${TAG}
cd /tmp/odoo-mcp-${TAG}

gh release download "${TAG}" \
  --repo deltix-consulting/odoo-mcp \
  --pattern '*.whl' \
  --pattern '*.tar.gz'

ls -la
# odoo_mcp-0.16.2-py3-none-any.whl
# odoo_mcp-0.16.2.tar.gz
```

## 3. Verify the build provenance attestation

```bash
gh attestation verify odoo_mcp-${TAG#v}-py3-none-any.whl \
  --repo deltix-consulting/odoo-mcp
```

You should see:

```
Loaded digest sha256:... for file://...
Loaded 1 attestation from GitHub API
✓ Verification succeeded!
```

A failure here is a **hard stop**. The artifact you downloaded was not produced by the deltix-consulting/odoo-mcp GitHub Actions release workflow on this tag. **Do not install.**

## 4. Compare SHA-256

```bash
sha256sum odoo_mcp-${TAG#v}-py3-none-any.whl
```

Compare the hash to the digest that `gh attestation verify` printed in step 3. They must match. If they do not, something went wrong between download and verification — re-download and try again.

## 5. Read the release notes

```bash
gh release view "${TAG}" --repo deltix-consulting/odoo-mcp
```

Specifically look for:

- A **`### Security`** heading in the body → there are security-sensitive changes you should review before installing
- Any **breaking change** notice
- The list of files in the release (should match what you downloaded)

## 6. Install from the verified wheel

```bash
uv tool install --force ./odoo_mcp-${TAG#v}-py3-none-any.whl
# or:
# pipx install ./odoo_mcp-${TAG#v}-py3-none-any.whl
```

You now have the verified release installed. From here, the normal flow continues — first-time setup via `odoo-mcp setup`, see [GETTING_STARTED.md](GETTING_STARTED.md).

## 7. Verify the installed binary

```bash
odoo-mcp --help     # should print the help with the right version
odoo-mcp doctor     # should authenticate against your Odoo
```

If `doctor` succeeds, your install is verified and ready.

## When to re-verify

- Every new release you install (`odoo-mcp update` runs an attestation check too, but doing it by hand on a tagged version is the high-assurance path).
- After any unexpected behaviour that could be a supply-chain compromise.
- Periodically — e.g. quarterly — for any long-running production install.

## What this flow does NOT cover

- The integrity of the **Odoo XML-RPC endpoint** you talk to (TLS verification is enforced by `odoo-mcp` itself, but the trust on the other end is yours).
- The integrity of your **OS credential store**. macOS Keychain / Windows Credential Manager / libsecret are the OS's responsibility, not ours.
- The integrity of every **transitive Python dependency** at runtime. CI runs `pip-audit` as an advisory; vulnerabilities there will surface in the Actions log but the maintainer team currently does not have a vuln-response SLA. See [SECURITY.md](SECURITY.md).
- A **third-party security audit**. None has been performed; the threat model is documented but not externally validated.

## Reporting a verification failure

If you cannot make this flow succeed on a release you downloaded from the GitHub UI, that is a potential supply-chain incident. Email `hello@deltix.pro` with subject `[odoo-mcp security]`. Include the tag, the file you downloaded, and the full `gh attestation verify` output.
