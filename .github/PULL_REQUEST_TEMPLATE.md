<!--
  Thanks for the PR. This template helps reviewers (and you) check the
  things that tend to bite us. Strike out items that don't apply, fill
  the rest. Keep PRs small and reviewable.
-->

## What this PR does

<!-- One paragraph. What changes, why, who it affects. -->

## Type of change

- [ ] Bug fix (non-breaking, fixes incorrect behaviour)
- [ ] New feature (non-breaking, adds capability)
- [ ] Breaking change (changes existing tool/argument/config surface)
- [ ] Security-sensitive change — denylist, redaction, prod-guard,
      credentials, audit log, or any other safety boundary
- [ ] Documentation / tooling / CI only

## Tests

- [ ] Existing tests still pass: `uv run --extra dev pytest -q`
- [ ] Added tests for new behaviour (or explain why not below)
- [ ] Lint clean: `uv run ruff check . && uv run ruff format --check .`
- [ ] Types clean: `uv run mypy src/odoo_mcp`

If no tests were added for a behaviour change, explain why:

<!-- e.g. "Doc-only PR; existing tests cover the affected codepath." -->

## Security impact

For security-sensitive changes, fill this in. Otherwise mark N/A.

- **Threat addressed or surface changed:**
- **Existing tests that pin the new behaviour:**
- **New tests added to pin it:**
- **Residual risk after this change:**

If this PR weakens or removes a guardrail (denylist entry, redaction
pattern, prod-write step, credential handling), call it out explicitly
here and explain why it is safe.

## Docs impact

- [ ] README / GETTING_STARTED / ONBOARDING updated if user-visible
- [ ] SECURITY.md updated if the threat model or any gate changed
- [ ] CHANGELOG.md updated with a `## [x.y.z]` section
- [ ] Security-sensitive changes flagged in the changelog entry

## Reviewer checklist

- [ ] Diff is small enough to review in one sitting
- [ ] No new env var / config key without docs
- [ ] No new outbound HTTP target beyond Odoo + GitHub
- [ ] No new dependency unless justified above
- [ ] CI green
