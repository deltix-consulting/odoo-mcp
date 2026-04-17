"""Shared helpers for the self-update feature.

Kept in its own module so both ``doctor`` and ``update_cli`` can reuse the
version-parsing / GitHub-latest-release logic without importing each other.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_LATEST_RELEASE_URL = "https://api.github.com/repos/deltix-consulting/odoo-mcp/releases/latest"
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
    """Fetch the latest release tag from GitHub. Returns None on any failure."""
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
