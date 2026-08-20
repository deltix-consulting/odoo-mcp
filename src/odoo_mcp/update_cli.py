"""Self-update command for odoo-mcp.

Usage::

    odoo-mcp update                       # fetch, verify, confirm, apply
    odoo-mcp update --check               # just report whether a newer version exists
    odoo-mcp update --skip-verification   # bypass attestation check (not recommended)

The update flow assumes a git checkout that runs the package via ``uv``. If
no ``pyproject.toml`` can be found by walking up from this file, the command
aborts — self-update from a wheel install is not supported.

Before the checkout moves, the latest release tarball's GitHub
build-provenance attestation is verified via ``gh attestation verify``. A
hard verification failure — ``gh`` ran and rejected the artifact, or the
artifact carries no attestation at all — refuses the update. Genuinely
environmental issues (no ``gh``, offline, Sigstore/TUF trouble) print a
yellow warning and prompt the user to confirm; ``--skip-verification``
bypasses the check entirely.

**The update lands on the verified release tag's commit, not the branch
tip.** Verifying a release tarball and then pulling ``origin/<branch>``
would check one artifact and install a different one — a compromised
branch tip would sail through, because nothing tied the verification to
the code being installed. Pinning to the tag is what makes the provenance
check meaningful. When the operator opts out of verification, the legacy
branch-tip behaviour applies.
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


def _tag_commit(project_dir: Path, tag: str) -> str | None:
    """Resolve a release tag to the commit it points at, or ``None``.

    ``{tag}^{{commit}}`` dereferences an annotated tag object down to the
    commit, so signed/annotated and lightweight tags both resolve the same
    way. Returns ``None`` when the tag is unknown locally (not fetched, or
    it does not exist upstream).
    """
    for ref in (f"refs/tags/{tag}^{{commit}}", f"{tag}^{{commit}}"):
        result = _git(project_dir, "rev-parse", "--verify", "--quiet", ref, check=False)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return None


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
    # Fetch the tag directly so we can distinguish "couldn't reach GitHub"
    # from "no newer release exists". The previous code conflated the two
    # and cheerfully printed "Up to date" whenever the anonymous GitHub
    # API rate-limit hit — hiding exactly the failure mode that made the
    # update prompt insecure.
    tag = fetch_latest_tag()
    if tag is None:
        print(
            f"{_YELLOW}Could not reach GitHub to check for updates "
            f"(network error or rate limit). You have {current_version}.{_RESET}\n"
            f"{_YELLOW}Tip: install the GitHub CLI and run `gh auth login` "
            f"to use the authenticated 5000/hour limit instead of the "
            f"anonymous 60/hour shared per IP.{_RESET}",
            file=sys.stderr,
        )
        return 1
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


def _handle_verification(skip: bool) -> tuple[bool, str | None]:
    """Verify the latest release's attestation.

    Returns ``(proceed, verified_tag)``. ``verified_tag`` is non-``None``
    only when ``gh`` positively confirmed the release artifact's provenance;
    the caller uses it to pin the checkout to that exact release, so the code
    we install is the code we verified. A ``None`` tag alongside
    ``proceed=True`` means the user consented to an unverified update
    (``--skip-verification``, or an environmental soft-fail they accepted).

    - ``skip=True``: print a notice, proceed unpinned.
    - Hard verification failure: print red error, refuse.
    - Environmental issue (no gh, offline): print yellow warning, prompt.
    - Verified: print confirmation, proceed pinned to the tag.
    """
    if skip:
        print(f"{_YELLOW}Skipping attestation verification (--skip-verification).{_RESET}")
        return (True, None)

    tag = fetch_latest_tag()
    if tag is None:
        print(
            f"{_YELLOW}Warning: could not determine latest release tag "
            f"(GitHub API unreachable). Attestation not verified.{_RESET}"
        )
        return (_confirm("Proceed without verification? [y/N]: "), None)

    print(f"Verifying build provenance attestation for {tag}...")
    verified, reason = verify_release_attestation(tag)
    if verified:
        print(f"  OK — {reason}")
        return (True, tag)

    if reason.startswith("environment:"):
        print(f"{_YELLOW}Warning: attestation verification could not run ({reason}).{_RESET}")
        return (_confirm("Proceed without verification? [y/N]: "), None)

    print(
        f"{_RED}ERROR: attestation verification failed ({reason}).{_RESET}\n"
        f"{_RED}Refusing update — the release artifact does not appear to be"
        f" signed by our CI workflow.{_RESET}",
        file=sys.stderr,
    )
    return (False, None)


def _resolve_update_target(
    project_dir: Path, branch: str, verified_tag: str | None, upstream: str
) -> tuple[str, str] | None:
    """Decide which commit to move to, and describe it.

    Returns ``(commit, description)`` or ``None`` to abort.

    When the attestation verified release tag T, the update must land on T's
    commit — NOT on the branch tip. Verifying a release tarball and then
    pulling ``origin/<branch>`` checks one artifact and installs a different
    one: a compromised branch tip sails through, because nothing ever tied
    the verification to the code being installed. Pinning to the tag is what
    makes the provenance check meaningful.

    If the branch tip is ahead of the tag, those extra commits carry no
    attestation; we say so and land on the tag anyway.
    """
    if verified_tag is None:
        # Unverified path (user consented): legacy branch-tip behaviour.
        return (upstream, f"origin/{branch}")

    commit = _tag_commit(project_dir, verified_tag)
    if commit is None:
        print(
            f"{_RED}ERROR: verified release {verified_tag} could not be resolved to a "
            f"commit in this checkout.{_RESET}\n"
            f"{_RED}Refusing update rather than falling back to the unverified "
            f"branch tip.{_RESET}",
            file=sys.stderr,
        )
        return None

    if commit != upstream:
        print(
            f"{_YELLOW}Note: origin/{branch} is not at the verified release "
            f"{verified_tag}. Updating to {verified_tag} ({commit[:12]}) — the commit "
            f"whose build provenance was just verified — and leaving the newer "
            f"unattested commits on {branch} alone.{_RESET}"
        )
    return (commit, f"{verified_tag} ({commit[:12]})")


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

    # Fetch. ``--tags`` so the verified release tag is resolvable locally —
    # the update pins to that tag's commit rather than the branch tip.
    fetch = _git(project_dir, "fetch", "--tags", "origin", check=False)
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
    proceed, verified_tag = _handle_verification(skip_verification)
    if not proceed:
        return 1

    # Resolve what we actually move to. On the verified path this is the
    # release tag's commit, so the code we install is the code whose
    # provenance we just checked.
    target = _resolve_update_target(project_dir, branch, verified_tag, upstream)
    if target is None:
        return 1
    target_commit, target_desc = target

    if target_commit == current:
        print(f"Already at the verified release {target_desc}.")
        return 0

    # Fast-forward only: refuses if the target isn't a descendant of HEAD,
    # so an update can never rewrite or discard local history.
    print(f"Updating to {target_desc}...")
    pull = _git(project_dir, "merge", "--ff-only", target_commit, check=False)
    if pull.returncode != 0:
        print(
            "Update failed — cannot fast-forward to the target commit. Resolve manually with git.",
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
