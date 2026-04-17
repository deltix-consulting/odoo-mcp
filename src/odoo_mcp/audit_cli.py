"""Audit log inspector CLI.

Invoked via ``python -m odoo_mcp audit``. Reads ``~/.odoo-mcp/audit.jsonl``
plus any rotated ``audit-YYYY-MM-DD.jsonl`` siblings, filters the entries
according to the caller's flags, and prints a fixed-width table.

Never prints credential values — the audit log itself is metadata-only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import DEFAULT_AUDIT_LOG

_ROTATED_PATTERN = re.compile(r"audit-\d{4}-\d{2}-\d{2}\.jsonl$")


def _audit_dir() -> Path:
    return Path(DEFAULT_AUDIT_LOG).expanduser().parent


def _audit_current() -> Path:
    return Path(DEFAULT_AUDIT_LOG).expanduser()


def _read_last_lines(path: Path, n: int) -> list[str]:
    """Return up to the last *n* non-empty lines from *path*.

    Small enough log that a straightforward read-all is fine; this avoids
    binary seek arithmetic and keeps the implementation trivial.
    """
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as f:
            lines = [line.rstrip("\n") for line in f if line.strip()]
    except OSError:
        return []
    return lines[-n:] if n > 0 else lines


def _load_all_entries() -> list[dict[str, Any]]:
    """Merge the current and rotated audit logs into a single list.

    Entries are parsed as JSON; malformed lines and open-markers are
    silently skipped. Returns entries sorted by timestamp ascending.
    """
    files: list[Path] = []
    cur = _audit_current()
    if cur.exists():
        files.append(cur)
    try:
        for entry in sorted(_audit_dir().iterdir()):
            if _ROTATED_PATTERN.match(entry.name):
                files.append(entry)
    except OSError:
        pass

    entries: list[dict[str, Any]] = []
    for f in files:
        for line in _read_last_lines(f, 0):
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("event") == "audit_log_open":
                continue
            if "tool" not in obj or "ts" not in obj:
                continue
            entries.append(obj)

    entries.sort(key=lambda e: str(e.get("ts", "")))
    return entries


def _parse_ts(value: str) -> datetime | None:
    try:
        # Format from audit.py: "%Y-%m-%dT%H:%M:%SZ"
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def _filter(
    entries: list[dict[str, Any]],
    *,
    errors_only: bool,
    instance: str | None,
    since_minutes: int | None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    out = entries
    if errors_only:
        # Errors-only implies "last 24h" per spec.
        current = now or datetime.now(tz=UTC)
        cutoff = current - timedelta(hours=24)
        out = [
            e
            for e in out
            if e.get("result") != "ok"
            and (ts := _parse_ts(str(e.get("ts", "")))) is not None
            and ts >= cutoff
        ]
    if instance is not None:
        out = [e for e in out if e.get("instance") == instance]
    if since_minutes is not None:
        current = now or datetime.now(tz=UTC)
        cutoff = current - timedelta(minutes=since_minutes)
        out = [
            e for e in out if (ts := _parse_ts(str(e.get("ts", "")))) is not None and ts >= cutoff
        ]
    return out


def _format_detail(entry: dict[str, Any]) -> str:
    rc = entry.get("record_count")
    dur = entry.get("duration_ms")
    parts: list[str] = []
    if isinstance(rc, int):
        parts.append(f"{rc} records")
    if isinstance(dur, int):
        parts.append(f"{dur}ms")
    details = entry.get("details")
    if isinstance(details, dict):
        err = details.get("error")
        if isinstance(err, str) and err:
            truncated = err if len(err) <= 80 else err[:77] + "..."
            parts.append(f"(error: {truncated})")
    return " ".join(parts)


def _render_table(entries: list[dict[str, Any]]) -> str:
    header = ("TIME", "RESULT", "TOOL", "INSTANCE", "MODEL", "DETAIL")
    rows: list[tuple[str, str, str, str, str, str]] = [header]
    for e in entries:
        rows.append(
            (
                str(e.get("ts", "")),
                str(e.get("result", "")),
                str(e.get("tool", "")),
                str(e.get("instance", "")),
                str(e.get("model") or "-"),
                _format_detail(e),
            )
        )
    # Compute column widths (excluding DETAIL which is last and free-form).
    widths = [0, 0, 0, 0, 0, 0]
    for row in rows:
        for i, cell in enumerate(row):
            if len(cell) > widths[i]:
                widths[i] = len(cell)
    out_lines: list[str] = []
    for row in rows:
        out_lines.append(
            "  ".join(
                row[i].ljust(widths[i]) if i < len(row) - 1 else row[i] for i in range(len(row))
            ).rstrip()
        )
    return "\n".join(out_lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="odoo-mcp audit",
        description="Inspect the Odoo MCP audit log.",
    )
    parser.add_argument(
        "--tail",
        type=int,
        default=20,
        help="Show the last N entries (default 20).",
    )
    parser.add_argument(
        "--errors",
        action="store_true",
        help="Only entries where result != 'ok' from the last 24 hours.",
    )
    parser.add_argument(
        "--instance",
        type=str,
        default=None,
        help="Filter to one instance name.",
    )
    parser.add_argument(
        "--since",
        type=int,
        default=None,
        help="Entries from the last N minutes.",
    )
    ns = parser.parse_args(argv)

    entries = _load_all_entries()
    filtered = _filter(
        entries,
        errors_only=ns.errors,
        instance=ns.instance,
        since_minutes=ns.since,
    )
    # `--tail` applies last, after filters.
    if ns.tail > 0:
        filtered = filtered[-ns.tail :]

    if not filtered:
        print("(no audit entries match the filters)")
        return 0

    print(_render_table(filtered))
    return 0


if __name__ == "__main__":
    sys.exit(main())
