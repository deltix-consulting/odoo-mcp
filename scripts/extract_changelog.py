"""Extract a single version's changelog section from CHANGELOG.md.

Usage:
    python scripts/extract_changelog.py <tag> <changelog_path> <output_path>

Given a tag like ``v0.2.0``, find the ``## [0.2.0] - YYYY-MM-DD`` block
and write its body to ``output_path``. If no matching block exists,
fall back to the ``[Unreleased]`` block. If neither exists, write a
placeholder line so the release body is never empty.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_HEADING_RE = re.compile(r"^## \[([^\]]+)\](?:\s*-\s*.*)?\s*$")


def extract_section(changelog: str, version: str) -> str | None:
    """Return the body (without the heading) for ``version`` or None."""
    lines = changelog.splitlines()
    start: int | None = None
    end: int | None = None
    for i, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match is None:
            continue
        if start is None:
            if match.group(1).strip().lower() == version.lower():
                start = i + 1
            continue
        # We are inside the target section and hit the next heading.
        end = i
        break
    if start is None:
        return None
    if end is None:
        end = len(lines)
    body = "\n".join(lines[start:end]).strip()
    return body or None


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        sys.stderr.write(
            "usage: extract_changelog.py <tag> <changelog_path> <output_path>\n",
        )
        return 2
    tag, changelog_path, output_path = argv[1], argv[2], argv[3]
    version = tag[1:] if tag.startswith("v") else tag
    text = Path(changelog_path).read_text(encoding="utf-8")

    body = extract_section(text, version)
    if body is None:
        body = extract_section(text, "Unreleased")
    if body is None:
        body = f"Release {tag}."

    Path(output_path).write_text(body + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
