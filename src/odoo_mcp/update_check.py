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


def _section_bounds(lines: list[str]) -> list[tuple[str, int, int]]:
    """Split *lines* into ``## [<label>]`` sections.

    Returns ``(label, body_start, body_end)`` per section, in file order.
    ``label`` is the raw bracket content — ``"Unreleased"`` or a version
    like ``"0.27.0"``.
    """
    heads: list[tuple[str, int]] = []
    for i, line in enumerate(lines):
        if not line.startswith("## ["):
            continue
        close = line.find("]", 4)
        if close == -1:
            continue
        heads.append((line[4:close].strip(), i))

    out: list[tuple[str, int, int]] = []
    for n, (label, i) in enumerate(heads):
        end = heads[n + 1][1] if n + 1 < len(heads) else len(lines)
        out.append((label, i + 1, end))
    return out


def _security_body(lines: list[str], start: int, end: int) -> str | None:
    """Return the ``### Security`` body within ``lines[start:end]``, if any."""
    sec_start = None
    for i in range(start, end):
        if lines[i].strip() == "### Security":
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


def extract_security_section(
    changelog_text: str,
    *,
    since_version: str | None = None,
) -> str | None:
    """Extract the ``### Security`` content this update brings in.

    Scans **every** ``## [<version>]`` section newer than *since_version*
    (newest first) and joins their Security bodies. Returns None when no
    such section exists or all of them are empty.

    Two things this deliberately does not do, both of which it used to:

    - **Stop at the first ``## [`` header.** Every release tag in this repo
      ships an empty ``## [Unreleased]`` on top of the CHANGELOG, so the
      first header is almost never a release — the scan found no
      ``### Security`` under it and reported "no security changes" for
      every update ever made, including the ones that were security
      releases.
    - **Look at one section only.** An update that spans several releases
      (0.24.0 -> 0.27.0) includes the Security notes of all of them, not
      just the newest.

    ``## [Unreleased]`` is always in scope: an update that lands on the
    branch tip installs it. At a release tag that section is empty and
    contributes nothing. When *since_version* is None or unparseable
    (e.g. the ``"dev"`` sentinel), the newest released section is scanned
    — erring toward showing the notice rather than withholding it.
    """
    lines = changelog_text.splitlines()
    sections = _section_bounds(lines)
    if not sections:
        return None

    since = _parse_version(since_version) if since_version else ()

    bodies: list[str] = []
    newest_release_seen = False
    for label, start, end in sections:
        if label.lower() == "unreleased":
            in_scope = True
        elif since:
            in_scope = _is_newer(since, _parse_version(label))
        else:
            # No usable baseline: the newest released section only.
            in_scope = not newest_release_seen
            newest_release_seen = True

        if not in_scope:
            continue
        body = _security_body(lines, start, end)
        if body:
            bodies.append(body)

    return "\n\n".join(bodies) or None


def read_changelog_security(
    project_dir: Path,
    *,
    since_version: str | None = None,
) -> str | None:
    """Read ``CHANGELOG.md`` from *project_dir* and extract its Security block.

    *since_version* is the version the caller is updating **from**; every
    released section newer than it is scanned. See
    :func:`extract_security_section` for what happens without it.
    """
    path = project_dir / "CHANGELOG.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return extract_security_section(text, since_version=since_version)
