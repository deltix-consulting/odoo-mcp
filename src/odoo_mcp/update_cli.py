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
    print("Update complete. Restart Claude Desktop / Cowork to use the new version.")
    return 0 if tests_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
