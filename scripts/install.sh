#!/usr/bin/env bash
# odoo-mcp bootstrap installer for macOS.
#
# Safe to pipe through bash:
#   curl -fsSL https://raw.githubusercontent.com/deltix-consulting/odoo-mcp/main/scripts/install.sh | bash
#
# Or run locally:
#   bash scripts/install.sh

set -euo pipefail

REPO="deltix-consulting/odoo-mcp"
DEFAULT_HOME="$HOME/odoo-mcp"
ODOO_MCP_HOME="${ODOO_MCP_HOME:-$DEFAULT_HOME}"
USE_GIT="${ODOO_MCP_INSTALL_GIT:-0}"
SKIP_VERIFICATION=0

for arg in "$@"; do
    case "$arg" in
        --git) USE_GIT=1 ;;
        --skip-verification) SKIP_VERIFICATION=1 ;;
        *) ;;
    esac
done

TOTAL_STEPS=9
step() {
    printf '\n[%s/%s] %s\n' "$1" "$TOTAL_STEPS" "$2"
}

fail() {
    printf '\nError: %s\n' "$1" >&2
    if [ -n "${2:-}" ]; then
        printf 'Fix: %s\n' "$2" >&2
    fi
    exit 1
}

# ----------------------------------------------------------------------
step 1 "Checking platform"
if [ "$(uname -s)" != "Darwin" ]; then
    fail "odoo-mcp currently supports macOS only (detected $(uname -s))." \
         "Run this installer on a macOS machine."
fi
echo "  macOS detected."

# ----------------------------------------------------------------------
step 2 "Checking for uv"
if ! command -v uv >/dev/null 2>&1; then
    echo "  uv not found. Installing via the official installer..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # The uv installer writes to ~/.local/bin and (optionally) ~/.cargo/bin.
    # Source its env file if present so the current shell sees uv.
    if [ -f "$HOME/.local/bin/env" ]; then
        # shellcheck disable=SC1091
        . "$HOME/.local/bin/env"
    fi
    if [ -f "$HOME/.cargo/env" ]; then
        # shellcheck disable=SC1091
        . "$HOME/.cargo/env"
    fi
    export PATH="$HOME/.local/bin:$PATH"
    if ! command -v uv >/dev/null 2>&1; then
        fail "uv was installed but is not on PATH." \
             "Open a new shell and re-run this installer."
    fi
else
    echo "  uv found: $(command -v uv)"
fi

# ----------------------------------------------------------------------
step 3 "Checking for gh CLI and authentication"
if ! command -v gh >/dev/null 2>&1; then
    # gh missing. Most macs that don't have gh also don't have brew (we
    # used to assume brew was always there — it isn't on a fresh OS).
    # Detect both, and offer to bootstrap brew from the official installer
    # only with explicit consent: the brew installer modifies the user's
    # system (writes to /opt/homebrew, edits shell rc files), so we never
    # run it silently.
    if ! command -v brew >/dev/null 2>&1; then
        echo "  Neither gh nor Homebrew is installed."
        if [ -t 0 ]; then
            printf '  Install Homebrew now? This runs the official installer\n'
            printf '  from https://brew.sh and may modify your shell config. [y/N] '
            read -r reply
            case "$reply" in
                y|Y|yes|YES) ;;
                *)
                    fail "Homebrew is required to install gh." \
                         "Install Homebrew first from https://brew.sh, then re-run this installer."
                    ;;
            esac
        else
            fail "Homebrew is required to install gh, and this is a non-interactive shell." \
                 "Install Homebrew first from https://brew.sh, then re-run this installer."
        fi
        echo "  Installing Homebrew via the official installer..."
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        # The brew installer prints which `eval` line to add. Apple Silicon
        # uses /opt/homebrew, Intel /usr/local. Source whichever exists so
        # the current shell sees brew.
        if [ -x /opt/homebrew/bin/brew ]; then
            eval "$(/opt/homebrew/bin/brew shellenv)"
        elif [ -x /usr/local/bin/brew ]; then
            eval "$(/usr/local/bin/brew shellenv)"
        fi
        if ! command -v brew >/dev/null 2>&1; then
            fail "Homebrew was installed but is not on PATH." \
                 "Open a new shell and re-run this installer."
        fi
        echo "  Homebrew installed: $(command -v brew)"
    fi
    echo "  Installing gh via Homebrew..."
    brew install gh
    if ! command -v gh >/dev/null 2>&1; then
        fail "gh installed but is not on PATH." \
             "Open a new shell and re-run this installer."
    fi
fi
if ! gh auth status >/dev/null 2>&1; then
    fail "gh CLI is not authenticated." \
         "Run: gh auth login"
fi
echo "  gh CLI authenticated."

# ----------------------------------------------------------------------
step 4 "Choosing install directory"
if [ -e "$ODOO_MCP_HOME" ]; then
    fail "Install directory already exists: $ODOO_MCP_HOME" \
         "To update an existing install, run: cd \"$ODOO_MCP_HOME\" && uv run odoo-mcp update"
fi
echo "  Will install to: $ODOO_MCP_HOME"

# ----------------------------------------------------------------------
step 5 "Fetching source"
FETCHED_VIA_RELEASE=0
TARBALL=""
TMPDIR_INSTALL=""
if [ "$USE_GIT" != "1" ]; then
    LATEST_TAG=""
    if LATEST_TAG="$(gh release view --repo "$REPO" --json tagName --jq .tagName 2>/dev/null)"; then
        :
    else
        LATEST_TAG=""
    fi
    if [ -n "$LATEST_TAG" ]; then
        echo "  Latest release: $LATEST_TAG"
        TMPDIR_INSTALL="$(mktemp -d /tmp/odoo-mcp-install.XXXXXX)"
        if gh release download "$LATEST_TAG" \
                --repo "$REPO" \
                --pattern "*.tar.gz" \
                --dir "$TMPDIR_INSTALL" >/dev/null 2>&1; then
            TARBALL="$(find "$TMPDIR_INSTALL" -maxdepth 1 -name '*.tar.gz' | head -n 1)"
            if [ -n "$TARBALL" ]; then
                FETCHED_VIA_RELEASE=1
                echo "  Downloaded $LATEST_TAG tarball"
            fi
        fi
        if [ "$FETCHED_VIA_RELEASE" = "0" ]; then
            echo "  Release download failed; falling back to git clone."
            rm -rf "$TMPDIR_INSTALL" 2>/dev/null || true
            TMPDIR_INSTALL=""
            TARBALL=""
        fi
    else
        echo "  No releases published yet; falling back to git clone."
    fi
fi

# ----------------------------------------------------------------------
step 6 "Verifying release attestation"
# Only applies to release tarballs. Git clones don't have attestations
# attached to them; trust there comes from the gh-authed repo access.
if [ "$FETCHED_VIA_RELEASE" = "1" ]; then
    if [ "$SKIP_VERIFICATION" = "1" ]; then
        printf '  \033[33mSkipping attestation verification (--skip-verification).\033[0m\n'
    else
        # `gh attestation verify` distinguishes hard verification failure
        # (signature mismatch) from environmental failure (offline, gh
        # not authed, attestation absent, attestation server 404'd) only
        # via exit code plus context. We err on the lenient side: if
        # `gh auth status` is OK we still treat several known-environmental
        # message patterns as soft-fails (warn + interactive prompt). The
        # only case that hard-fails is exit-code-1 with output that
        # matches NONE of those patterns — which is what a real signature
        # mismatch looks like. Trade-off: a tampered tarball whose
        # attestation server happens to 404 would slip past, which is
        # extremely unlikely; in exchange, releases made while the repo
        # was private (no attestation ever published) install cleanly.
        # See pilot blocker B2 (v0.13.1) for the rationale.
        if gh auth status >/dev/null 2>&1; then
            VERIFY_OUTPUT=""
            if VERIFY_OUTPUT="$(gh attestation verify \
                    --owner deltix-consulting \
                    --signer-workflow ".github/workflows/release.yml" \
                    "$TARBALL" 2>&1)"; then
                echo "  Attestation verified."
            else
                # Patterns that mean "no attestation could be retrieved"
                # (rather than "the attestation says this tarball is
                # tampered"). Case-insensitive grep across all known
                # variants we've seen from gh / GitHub's API: missing,
                # 404, generic fetch failure.
                if printf '%s' "$VERIFY_OUTPUT" | grep -qiE 'no[[:space:]].*attestation|404|not found|failed to fetch'; then
                    printf '\n  \033[33mWarning:\033[0m no attestation available for %s.\n' "$LATEST_TAG"
                    printf '  Reason: %s\n' "$(printf '%s' "$VERIFY_OUTPUT" | head -n 1)"
                    printf '  This can happen on free-tier GitHub orgs, for releases\n'
                    printf '  cut while the repo was private, or when GitHub'\''s attestation\n'
                    printf '  service is unreachable.\n'
                    if [ -t 0 ]; then
                        printf '  Proceed anyway? [y/N] '
                        read -r reply
                        case "$reply" in
                            y|Y|yes|YES) ;;
                            *) fail "Aborted by user." "Re-run with --skip-verification to bypass." ;;
                        esac
                    else
                        printf '  \033[33mNon-interactive shell; proceeding.\033[0m\n'
                    fi
                else
                    printf '\n  \033[31mAttestation verification FAILED for %s:\033[0m\n' "$LATEST_TAG"
                    printf '%s\n' "$VERIFY_OUTPUT" | sed 's/^/    /'
                    fail "Refusing to install an unverified release tarball." \
                         "If this is unexpected, file an issue. To bypass for environmental reasons only, re-run with --skip-verification."
                fi
            fi
        else
            printf '  \033[33mWarning:\033[0m gh CLI is not authenticated; skipping attestation check.\n'
            printf '  This is treated as an environmental failure (same policy as odoo-mcp update).\n'
        fi
    fi
    mkdir -p "$ODOO_MCP_HOME"
    tar -xzf "$TARBALL" -C "$ODOO_MCP_HOME" --strip-components=1
    rm -rf "$TMPDIR_INSTALL"
    echo "  Extracted $LATEST_TAG into $ODOO_MCP_HOME"
else
    echo "  Skipped (no release tarball — using git clone)."
    gh repo clone "$REPO" "$ODOO_MCP_HOME" -- --quiet
    echo "  Cloned $REPO into $ODOO_MCP_HOME"
fi

# ----------------------------------------------------------------------
step 7 "Installing Python dependencies (uv sync)"
cd "$ODOO_MCP_HOME"
uv sync

# ----------------------------------------------------------------------
step 8 "Installing odoo-mcp on PATH (uv tool)"
# Without this the launcher.sh that Claude Cowork runs works fine, but
# the user has no way to call `odoo-mcp doctor` / `odoo-mcp status` /
# `odoo-mcp update` from anywhere except inside the project directory.
# `uv tool install --editable` puts a thin wrapper into ~/.local/bin
# that points at this checkout, so updates via `git pull` are picked up
# without re-running `uv tool install`.
uv tool install --editable . --force >/dev/null
echo "  odoo-mcp CLI installed (run 'odoo-mcp --help' to verify)"

# Make ~/.local/bin available NOW (so the setup wizard launched below
# can resolve `odoo-mcp` without a shell restart) AND persist it for
# future shells. The persisted line is appended only if it isn't
# already present, so re-running the installer doesn't pile up
# duplicates.
LOCAL_BIN="$HOME/.local/bin"
case ":$PATH:" in
    *":$LOCAL_BIN:"*) ;;
    *) export PATH="$LOCAL_BIN:$PATH" ;;
esac
PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
case "${SHELL:-}" in
    */zsh)  RC="$HOME/.zshrc" ;;
    */bash) RC="$HOME/.bashrc" ;;
    *)      RC="$HOME/.zshrc" ;;  # macOS default
esac
if ! command -v odoo-mcp >/dev/null 2>&1; then
    echo "  Warning: 'odoo-mcp' is not on PATH after install."
fi
if [ -f "$RC" ] && grep -Fxq "$PATH_LINE" "$RC" 2>/dev/null; then
    : # already present
else
    printf '\n# Added by odoo-mcp installer\n%s\n' "$PATH_LINE" >> "$RC"
    echo "  Added '$LOCAL_BIN' to PATH in $RC."
    echo "  Run 'source $RC' (or restart your terminal) before using odoo-mcp commands."
fi

# ----------------------------------------------------------------------
step 9 "Launching setup wizard"
echo "  Install complete. Running setup wizard..."
exec uv run odoo-mcp setup
