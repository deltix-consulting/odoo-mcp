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
    fail "gh CLI is required to install from a private repo." \
         "Install it and authenticate: brew install gh && gh auth login"
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
        # not authed, attestation not yet published) only via exit code
        # plus context. We treat the situation pragmatically: if
        # `gh auth status` succeeds we already know gh is wired up, so
        # any non-zero exit from `gh attestation verify` is treated as a
        # hard failure. If gh auth is broken, we treat the whole step
        # as environmental — same soft-fail policy as
        # `odoo-mcp update`.
        if gh auth status >/dev/null 2>&1; then
            VERIFY_OUTPUT=""
            if VERIFY_OUTPUT="$(gh attestation verify \
                    --owner deltix-consulting \
                    --signer-workflow ".github/workflows/release.yml" \
                    "$TARBALL" 2>&1)"; then
                echo "  Attestation verified."
            else
                # Distinguish: if the message mentions "no attestations
                # found" we treat it as environmental (free-tier orgs
                # may have attestations disabled per 0.6.1). Anything
                # else is a hard failure.
                if printf '%s' "$VERIFY_OUTPUT" | grep -qi "no attestations"; then
                    printf '\n  \033[33mWarning:\033[0m no attestations found for %s.\n' "$LATEST_TAG"
                    printf '  This can happen on free-tier GitHub orgs.\n'
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
# Make sure ~/.local/bin is on PATH for the current shell session.
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *)
        echo
        echo "  NOTE: '$HOME/.local/bin' is not on your PATH yet."
        echo "  Add this line to ~/.zshrc (or ~/.bash_profile):"
        echo "      export PATH=\"\$HOME/.local/bin:\$PATH\""
        echo "  Then open a new terminal so 'odoo-mcp' resolves correctly."
        ;;
esac

# ----------------------------------------------------------------------
step 9 "Launching setup wizard"
echo "  Install complete. Running setup wizard..."
exec uv run odoo-mcp setup
