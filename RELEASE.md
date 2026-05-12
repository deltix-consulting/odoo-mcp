# Release checklist

A release of `odoo-mcp` is a tag push that triggers
[`.github/workflows/release.yml`](.github/workflows/release.yml). The
workflow handles build, tests, attestation, and the GitHub Release. The
checklist below covers the human pieces.

## Pre-release

- [ ] **Tests green locally.** Run:

  ```bash
  uv run --extra dev ruff check .
  uv run --extra dev ruff format --check .
  uv run --extra dev mypy src/odoo_mcp
  uv run --extra dev pytest -q
  ```

- [ ] **CHANGELOG.md** has a `## [x.y.z] - YYYY-MM-DD` section for the
  release. Sections that the audience cares about are `### Added`,
  `### Changed`, `### Fixed`, and **`### Security`** (the latter must
  appear if any of: denylist changes, field redaction changes,
  prod-write flow changes, credential handling changes, or audit-log
  changes happened).

- [ ] **`pyproject.toml` version** matches the tag you're about to push.

- [ ] **Docs reviewed:**
  - README compatibility matrix still correct
  - GETTING_STARTED gotcha list still relevant
  - SECURITY.md threat-model matrix updated if any guard changed
  - VERIFY.md unchanged unless the attestation flow itself changed

- [ ] **Security-sensitive change?** Reach for an extra reviewer before
  tagging. Specifically:
  - Anything that adds or removes a `MODEL_DENYLIST` entry
  - Anything that changes `_ALWAYS_REDACTED_PATTERNS` or
    `_DEFAULT_HIDDEN`
  - Anything that changes the prod-guard flow (unlock, dry-run,
    token, burst budget)
  - Anything that touches credential storage or env-var purge
  - Anything that changes audit-log shape

- [ ] **Breaking change?** Major bump (`0.x.y` → `0.(x+1).0`) plus
  prominent notice in the changelog.

## Tag and push

```bash
TAG=v0.16.3
git tag -a "${TAG}" -m "${TAG} — one-line summary"
git push origin main
git push origin "${TAG}"
```

The release workflow now runs.

## Post-release verification

- [ ] **CI green:** check the Release workflow run in GitHub Actions.
  Both lint/test and build-attestation steps must be green. **The
  attestation step is hard-fail** — a failure there means no release
  is published.

- [ ] **GitHub Release published** with the wheel + sdist attached and
  the right changelog section as the body. `gh release view ${TAG}`.

- [ ] **Attestation verifies from the outside:**

  ```bash
  cd /tmp
  gh release download "${TAG}" --repo deltix-consulting/odoo-mcp \
      --pattern '*.whl'
  gh attestation verify ./odoo_mcp-*-py3-none-any.whl \
      --repo deltix-consulting/odoo-mcp
  ```

  This is the same flow a security-conscious operator follows — if it
  fails for you, it fails for them. See [VERIFY.md](VERIFY.md).

- [ ] **Update one real machine** to the new release and run
  `odoo-mcp doctor`. Catches "I broke the wrapper but tests didn't
  notice" regressions before they reach users.

- [ ] **Communicate** to the people using the install:
  - For security-sensitive changes: dedicated message + recommend
    re-running `odoo-mcp doctor` after update.
  - For regular changes: a one-liner pointing at the changelog.

## If the release workflow fails

Two common modes:

**Lint / mypy / tests fail in CI but passed locally.** Almost always a
ruff / format issue introduced by a last-second edit. Fix on `main`,
delete the tag, push again:

```bash
git tag -d "${TAG}"
git push origin :refs/tags/"${TAG}"
# fix the issue, commit
git tag -a "${TAG}" -m "${TAG} — same summary"
git push origin "${TAG}"
```

**Attestation step fails.** Usually a transient GitHub API issue.
Delete the tag and push again as above. If it fails consistently:

- Has the repo been made private? Attestations need a public repo or
  a paid org plan.
- Are workflow permissions intact? `contents: write`, `id-token: write`,
  `attestations: write` must all be in `release.yml`.
- Is the artefact path right? Default is `dist/*` and `uv build` puts
  files there.

## Optional: PyPI publish

Not yet automated — current model is "verified release from GitHub". See
[ROADMAP.md](ROADMAP.md) for the open decision on PyPI ownership.

If you decide to publish manually for a specific release:

```bash
uv build
uv run twine upload dist/*
```

You will need an API token with write rights on the `odoo-mcp` PyPI
project. Document who has those tokens.

## Optional: Docker publish

Not yet shipping a Docker image. See [ROADMAP.md](ROADMAP.md) for the
open decision. If/when added, this checklist will gain a step for it.
