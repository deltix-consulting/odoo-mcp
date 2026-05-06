"""Self-update command for odoo-mcp.

Usage::

    odoo-mcp update                       # fetch, verify, confirm, apply
    odoo-mcp update --check               # just report whether a newer version exists
    odoo-mcp update --skip-verification   # bypass attestation check (not recommended)

The update flow assumes a git checkout that runs the package via ``uv``. If
no ``pyproject.toml`` can be found by walking up from this file, the command
aborts — self-update from a wheel install is not supported.

Before any ``git pull`` happens, the latest release tarball's GitHub
build-provenance attestation is verified via ``gh attestation verify``. A
hard verification failure (``gh`` ran and rejected the artifact) refuses
the update. Environmental issues (no ``gh``, offline, GitHub down) print
a yellow warning and prompt the user to confirm; ``--skip-verification``
bypasses the check entirely.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from . import __version__
from .attestation import verify_release_attestation
from .update_check import check_for_update, fetch_latest_tag, read_changelog_security

_GIT = "/usr/bin/git"  # absolute path per security policy

# ANSI color codes for warnings / errors. Kept simple — terminals that
# don't support them just see the escape sequences, which is fine.
_RED = "\033[31m"
_YELLOW = "\033[33m"
_RESET = "\033[0m"


def _find_project_dir() -> Path | None:
    """Walk up from this file looking for ``pyproject.toml``."""
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        candidate = parent / "pyproject.toml"
        if candidate.is_file():
            return parent
    return None


def _git(project_dir: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a git command in *project_dir* and capture its output."""
    return subprocess.run(  # noqa: S603, S607 — git at absolute path, no shell
        [_GIT, "-C", str(project_dir), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _current_commit(project_dir: Path) -> str:
    return _git(project_dir, "rev-parse", "HEAD").stdout.strip()


def _current_branch(project_dir: Path) -> str:
    return _git(project_dir, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _upstream_commit(project_dir: Path, branch: str) -> str:
    return _git(project_dir, "rev-parse", f"origin/{branch}").stdout.strip()


def _has_local_changes(project_dir: Path) -> bool:
    out = _git(project_dir, "status", "--porcelain").stdout
    return bool(out.strip())


def _maybe_migrate_launcher() -> None:
    """Migrate from a legacy ``~/.odoo-mcp/launch.sh`` to direct ``odoo-mcp launch``.

    Pre-v0.13.0 installs registered Claude Desktop with a wrapper shell
    script that loaded Keychain creds and exec'd the server. v0.13.0
    drops the wrapper entirely: Claude Desktop calls the ``odoo-mcp``
    CLI directly with ``args: ["launch"]``, and the cross-platform
    credential store handles credential resolution in-process.

    Migration order matters: we MUST rewrite the Claude Desktop
    registration BEFORE deleting ``launch.sh``. If we delete first and
    the rewrite fails, Claude Desktop is left pointing at a missing
    file — broken until the user manually edits the JSON.

    Detection is loose: if the registered ``command`` references
    ``launch.sh`` ANYWHERE (substring match), we treat it as a legacy
    entry and force a rewrite. This handles symlink-resolved paths,
    relative paths, and other quirks the strict equality check missed.
    If no matching registration is found we log a warning and leave
    launch.sh in place — better a stale script than a broken config.
    """
    import json

    from .setup_wizard import _CLAUDE_DESKTOP_CONFIG, _LAUNCH_SH, _register_claude_desktop

    if not _LAUNCH_SH.exists():
        return

    legacy_entry_found = False
    config_data: dict[str, object] | None = None
    if _CLAUDE_DESKTOP_CONFIG.exists():
        try:
            loaded = json.loads(_CLAUDE_DESKTOP_CONFIG.read_text())
        except (OSError, ValueError):
            loaded = None
        if isinstance(loaded, dict):
            config_data = loaded
            servers = loaded.get("mcpServers")
            if isinstance(servers, dict):
                entry = servers.get("odoo-mcp")
                if isinstance(entry, dict):
                    command = entry.get("command")
                    if isinstance(command, str) and "launch.sh" in command:
                        legacy_entry_found = True

    if not legacy_entry_found:
        print(
            f"Warning: legacy {_LAUNCH_SH} found but no matching Claude "
            f"Desktop registration references it. Leaving the script in "
            f"place — remove it manually if no longer needed."
        )
        return

    # Rewrite Claude Desktop registration BEFORE deleting launch.sh.
    try:
        _register_claude_desktop()
    except OSError as exc:
        print(
            f"{_RED}ERROR: could not rewrite Claude Desktop registration "
            f"({exc}).{_RESET}\n"
            f"Aborting launcher migration; {_LAUNCH_SH} left in place so "
            f"Claude Desktop continues to work."
        )
        return

    # Verify the rewrite landed: re-read the config and confirm command no
    # longer references launch.sh. If verification fails, do NOT delete the
    # script — the user is better off with a working stale launcher than a
    # config pointing at a missing file.
    try:
        verify = json.loads(_CLAUDE_DESKTOP_CONFIG.read_text())
    except (OSError, ValueError) as exc:
        print(
            f"{_RED}ERROR: could not re-read Claude Desktop config after "
            f"rewrite ({exc}).{_RESET}\n"
            f"Aborting launcher migration; {_LAUNCH_SH} left in place."
        )
        return
    new_command = ""
    if isinstance(verify, dict):
        new_servers = verify.get("mcpServers")
        if isinstance(new_servers, dict):
            new_entry = new_servers.get("odoo-mcp")
            if isinstance(new_entry, dict):
                cmd = new_entry.get("command")
                if isinstance(cmd, str):
                    new_command = cmd
    if "launch.sh" in new_command:
        print(
            f"{_RED}ERROR: Claude Desktop registration rewrite did not take "
            f"effect — command still references launch.sh.{_RESET}\n"
            f"Aborting launcher migration; {_LAUNCH_SH} left in place."
        )
        # Reference config_data so static analyzers see it's load-bearing.
        del config_data
        return

    try:
        _LAUNCH_SH.unlink()
    except OSError as exc:
        print(f"Warning: could not remove legacy {_LAUNCH_SH} ({exc}).")
        return

    print(
        "Migrated launcher: Claude Desktop config now registers "
        "'odoo-mcp launch' directly; legacy launch.sh removed."
    )


def _maybe_register_codex() -> None:
    """Register the MCP in Codex when Codex is present on this machine."""
    from .setup_wizard import _register_codex

    try:
        _register_codex()
    except OSError as exc:
        print(f"Warning: could not update Codex registration ({exc}).")


def _print_check(current_version: str) -> int:
    result = check_for_update(current_version)
    if result is None:
        print(f"Up to date (version {current_version}).")
        return 0
    current, latest = result
    print(f"Update available: {latest} (you have {current}).")
    return 0


def _confirm(prompt: str) -> bool:
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run an arbitrary command (e.g. ``uv``) with output captured."""
    return subprocess.run(  # noqa: S603 — argv list, no shell
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def _handle_verification(skip: bool) -> bool:
    """Verify the latest release's attestation. Returns True if we should proceed.

    - ``skip=True``: print a notice, return True.
    - Hard verification failure: print red error, return False.
    - Environmental issue (no gh, offline): print yellow warning, prompt user.
    - Verified: print confirmation, return True.
    """
    if skip:
        print(f"{_YELLOW}Skipping attestation verification (--skip-verification).{_RESET}")
        return True

    tag = fetch_latest_tag()
    if tag is None:
        print(
            f"{_YELLOW}Warning: could not determine latest release tag "
            f"(GitHub API unreachable). Attestation not verified.{_RESET}"
        )
        return _confirm("Proceed without verification? [y/N]: ")

    print(f"Verifying build provenance attestation for {tag}...")
    verified, reason = verify_release_attestation(tag)
    if verified:
        print(f"  OK — {reason}")
        return True

    if reason.startswith("environment:"):
        print(f"{_YELLOW}Warning: attestation verification could not run ({reason}).{_RESET}")
        return _confirm("Proceed without verification? [y/N]: ")

    print(
        f"{_RED}ERROR: attestation verification failed ({reason}).{_RESET}\n"
        f"{_RED}Refusing update — the release artifact does not appear to be"
        f" signed by our CI workflow.{_RESET}",
        file=sys.stderr,
    )
    return False


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])

    if args and args[0] in {"-h", "--help"}:
        print(__doc__)
        return 0

    if args and args[0] == "--check":
        return _print_check(__version__)

    skip_verification = False
    if "--skip-verification" in args:
        skip_verification = True
        args = [a for a in args if a != "--skip-verification"]

    project_dir = _find_project_dir()
    if project_dir is None:
        print(
            "Cannot find project root — are you running from a git checkout?",
            file=sys.stderr,
        )
        return 1

    # Refuse to clobber local work.
    if _has_local_changes(project_dir):
        print(
            "Update aborted: uncommitted local changes detected. Commit or stash them first.",
            file=sys.stderr,
        )
        return 1

    # Fetch.
    fetch = _git(project_dir, "fetch", "origin", check=False)
    if fetch.returncode != 0:
        print(f"git fetch failed: {fetch.stderr.strip()}", file=sys.stderr)
        return 1

    try:
        branch = _current_branch(project_dir)
        current = _current_commit(project_dir)
        upstream = _upstream_commit(project_dir, branch)
    except subprocess.CalledProcessError as exc:
        print(f"git rev-parse failed: {exc.stderr}", file=sys.stderr)
        return 1

    if current == upstream:
        print("Already up to date.")
        return 0

    log = _git(
        project_dir,
        "log",
        "--oneline",
        f"{current}..{upstream}",
        check=False,
    )
    if log.stdout.strip():
        print("Incoming commits:")
        print(log.stdout.rstrip())
        print()

    if not _confirm("Apply update? [y/N]: "):
        print("Aborted.")
        return 0

    # Verify the latest release's build provenance before touching the repo.
    if not _handle_verification(skip_verification):
        return 1

    # Fast-forward pull.
    pull = _git(project_dir, "pull", "--ff-only", "origin", branch, check=False)
    if pull.returncode != 0:
        print(
            "Update failed — local changes detected. Resolve manually with git.",
            file=sys.stderr,
        )
        if pull.stderr.strip():
            print(pull.stderr.rstrip(), file=sys.stderr)
        return 1

    # Sync dependencies.
    sync = _run(["uv", "sync"], cwd=project_dir)
    if sync.returncode != 0:
        print("uv sync failed:", file=sys.stderr)
        print(sync.stderr.rstrip(), file=sys.stderr)
        return 1

    # Auto-migrate the launch.sh template if it still uses the old
    # two-process `launch-env` pattern. New (v0.7.0+) launchers go
    # through `python -m odoo_mcp launch` which loads Keychain creds
    # in-process. Existing users get the speed boost without manual
    # action.
    _maybe_migrate_launcher()
    _maybe_register_codex()

    # Refresh the user-installed CLI shim so `odoo-mcp` on PATH points at
    # the new version. `uv tool install --editable` resolves to a wrapper
    # that imports from the checkout, so a fresh git pull is technically
    # already live, but re-installing makes sure entry-point metadata
    # (new subcommands, version) is picked up.
    _run(["uv", "tool", "install", "--editable", str(project_dir), "--force"], cwd=project_dir)

    # Run the test suite.
    tests = _run(["uv", "run", "pytest", "-q"], cwd=project_dir)
    tests_ok = tests.returncode == 0
    if not tests_ok:
        print("!" * 60)
        print(
            "Update applied but tests are failing. Consider rolling back with:\n"
            f"  git -C {project_dir} reset --hard {current}"
        )
        print("!" * 60)

    # Changelog security highlight.
    security = read_changelog_security(project_dir)
    if security:
        print()
        print("=" * 60)
        print("SECURITY: This update includes security-relevant changes. Review CHANGELOG.md.")
        print("=" * 60)

    # Run doctor automatically.
    print()
    print("Running doctor...")
    from . import doctor

    doctor.main([])

    print()
    print("Update complete. Restart Claude Desktop / Cowork and Codex to use the new version.")
    return 0 if tests_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
