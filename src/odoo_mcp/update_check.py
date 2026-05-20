"""Shared helpers for the self-update feature.

Kept in its own module so both ``doctor`` and ``update_cli`` can reuse the
version-parsing / GitHub-latest-release logic without importing each other.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_REPO_SLUG = "deltix-consulting/odoo-mcp"
_LATEST_RELEASE_URL = f"https://api.github.com/repos/{_REPO_SLUG}/releases/latest"
_HTTP_TIMEOUT_SECONDS = 5.0


def _parse_version(raw: str) -> tuple[int, ...]:
    """Parse a version string like ``"v0.2.0"`` into ``(0, 2, 0)``.

    Non-numeric trailing components (e.g. ``"0.2.0rc1"``) stop the parse at
    the last fully numeric segment. The sentinel value ``"dev"`` yields an
    empty tuple so it never compares as newer than a real release.
    """
    if not raw:
        return ()
    s = raw.strip()
    if s.startswith(("v", "V")):
        s = s[1:]
    if s == "dev" or not s:
        return ()
    parts: list[int] = []
    for chunk in s.split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
        # If the chunk had a non-numeric tail, stop after this segment.
        if len(digits) != len(chunk):
            break
    return tuple(parts)


def _is_newer(current: tuple[int, ...], candidate: tuple[int, ...]) -> bool:
    """Return True if *candidate* is strictly greater than *current*."""
    if not candidate:
        return False
    if not current:
        # Unknown local version (e.g. "dev") — do not nag.
        return False
    return candidate > current


def fetch_latest_tag(url: str = _LATEST_RELEASE_URL) -> str | None:
    """Fetch the latest release tag from GitHub. Returns None on any failure.

    Tries authenticated ``gh`` CLI first (5000/hour rate limit), falls
    back to anonymous ``urllib`` (60/hour shared per IP — easy to hit
    from a corporate NAT, an over-eager update loop, or just bad luck).
    The fallback path is identical to the historical behaviour, so
    users without ``gh`` are no worse off than before — but the
    realistic case (the installer required ``gh auth login``) avoids
    the rate-limit cliff that turned the install-verify prompt into
    a "press y to ignore" reflex.
    """
    tag = _fetch_latest_tag_via_gh()
    if tag is not None:
        return tag
    return _fetch_latest_tag_via_urllib(url)


def _fetch_latest_tag_via_gh() -> str | None:
    """Use the authenticated ``gh`` CLI. Returns None if gh isn't usable."""
    gh_path = shutil.which("gh")
    if gh_path is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603 — gh resolved via shutil.which
            [
                gh_path,
                "release",
                "view",
                "--repo",
                _REPO_SLUG,
                "--json",
                "tagName",
                "--jq",
                ".tagName",
            ],
            capture_output=True,
            text=True,
            timeout=_HTTP_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    tag = result.stdout.strip()
    return tag or None


def _fetch_latest_tag_via_urllib(url: str) -> str | None:
    """Anonymous fallback. Hits the unauth GitHub API rate limit easily."""
    try:
        req = urllib.request.Request(  # noqa: S310 — https URL is hard-coded
            url,
            headers={"Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:  # noqa: S310
            payload = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    try:
        data = json.loads(payload.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeError):
        return None
    tag = data.get("tag_name") if isinstance(data, dict) else None
    return tag if isinstance(tag, str) and tag else None


def check_for_update(current_version: str) -> tuple[str, str] | None:
    """Return ``(current, latest)`` if an update is available, else None.

    Returns None when the network is unavailable, the API response is
    malformed, or the local version is already at or above the latest.
    """
    tag = fetch_latest_tag()
    if tag is None:
        return None
    latest = _parse_version(tag)
    current = _parse_version(current_version)
    if _is_newer(current, latest):
        latest_str = tag[1:] if tag.startswith(("v", "V")) else tag
        return (current_version, latest_str)
    return None


def extract_security_section(changelog_text: str) -> str | None:
    """Extract the ``### Security`` block from the most recent release.

    Scans for the first ``## [``-prefixed version header and returns the
    body of a ``### Security`` subsection that appears before the next
    ``## [`` header. Returns None if no such section exists or is empty.
    """
    lines = changelog_text.splitlines()
    # Find first top-level version header.
    start = None
    for i, line in enumerate(lines):
        if line.startswith("## ["):
            start = i
            break
    if start is None:
        return None

    # Find end of this version's block.
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## ["):
            end = i
            break

    # Find the Security subsection.
    sec_start = None
    for i in range(start + 1, end):
        stripped = lines[i].strip()
        if stripped == "### Security":
            sec_start = i + 1
            break
    if sec_start is None:
        return None

    sec_end = end
    for i in range(sec_start, end):
        if lines[i].startswith("### "):
            sec_end = i
            break

    body = "\n".join(lines[sec_start:sec_end]).strip()
    return body or None


def read_changelog_security(project_dir: Path) -> str | None:
    """Read ``CHANGELOG.md`` from *project_dir* and extract its Security block."""
    path = project_dir / "CHANGELOG.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return extract_security_section(text)
