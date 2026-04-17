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

for arg in "$@"; do
    case "$arg" in
        --git) USE_GIT=1 ;;
        *) ;;
    esac
done

TOTAL_STEPS=7
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
                mkdir -p "$ODOO_MCP_HOME"
                tar -xzf "$TARBALL" -C "$ODOO_MCP_HOME" --strip-components=1
                FETCHED_VIA_RELEASE=1
                rm -rf "$TMPDIR_INSTALL"
                echo "  Extracted $LATEST_TAG into $ODOO_MCP_HOME"
            fi
        fi
        if [ "$FETCHED_VIA_RELEASE" = "0" ]; then
            echo "  Release download failed; falling back to git clone."
            rm -rf "$TMPDIR_INSTALL" "$ODOO_MCP_HOME" 2>/dev/null || true
        fi
    else
        echo "  No releases published yet; falling back to git clone."
    fi
fi
if [ "$FETCHED_VIA_RELEASE" = "0" ]; then
    gh repo clone "$REPO" "$ODOO_MCP_HOME" -- --quiet
    echo "  Cloned $REPO into $ODOO_MCP_HOME"
fi

# ----------------------------------------------------------------------
step 6 "Installing Python dependencies (uv sync)"
cd "$ODOO_MCP_HOME"
uv sync

# ----------------------------------------------------------------------
step 7 "Launching setup wizard"
echo "  Install complete. Running setup wizard..."
exec uv run odoo-mcp setup
