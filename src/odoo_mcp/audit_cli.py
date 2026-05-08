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
_DATED_PATTERN = re.compile(r"audit-(\d{4}-\d{2}-\d{2})\.jsonl$")


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


def _audit_files(*, since_minutes: int | None = None) -> list[Path]:
    """Return the audit log files to scan, newest-first by date.

    The current ``audit.jsonl`` is always included (it holds today's
    entries). Rotated ``audit-YYYY-MM-DD.jsonl`` files are filtered to
    those whose date is within ``since_minutes`` of now — older ones
    cannot contain matching entries, so opening them is wasted work.

    With ``since_minutes=None`` every rotated file is included (the
    pre-v0.15.4 behaviour, used by ``--stats`` over the full history).
    """
    files: list[Path] = []
    cur = _audit_current()
    if cur.exists():
        files.append(cur)

    cutoff_date = None
    if since_minutes is not None and since_minutes >= 0:
        cutoff = datetime.now(tz=UTC) - timedelta(minutes=since_minutes)
        cutoff_date = cutoff.date()

    try:
        for entry in sorted(_audit_dir().iterdir()):
            m = _DATED_PATTERN.match(entry.name)
            if not m:
                continue
            if cutoff_date is not None:
                try:
                    file_date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
                except ValueError:
                    continue
                if file_date < cutoff_date:
                    continue
            files.append(entry)
    except OSError:
        pass
    return files


def _load_all_entries(*, since_minutes: int | None = None) -> list[dict[str, Any]]:
    """Merge the current and rotated audit logs into a single list.

    When ``since_minutes`` is set, rotated files older than that window
    are skipped — a real win on installs that have been running for
    weeks. Entries within the kept files are still returned in full;
    final ``--since`` filtering happens in :func:`_filter`. Entries are
    parsed as JSON; malformed lines and open-markers are silently
    skipped. Returns entries sorted by timestamp ascending.
    """
    files = _audit_files(since_minutes=since_minutes)
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


def _percentile(sorted_values: list[int], pct: float) -> int:
    """Nearest-rank percentile on a pre-sorted list of integers.

    Returns ``0`` for an empty list. ``pct`` is in 0..100. Cheap and
    deterministic — for the sample sizes we deal with (audit logs typically
    hold thousands of rows, not millions), this beats pulling in a stats
    library.
    """
    if not sorted_values:
        return 0
    k = max(0, min(len(sorted_values) - 1, int(round((pct / 100) * (len(sorted_values) - 1)))))
    return sorted_values[k]


def _stats_payload(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-tool stats as a JSON-serialisable list.

    Same data the human renderer shows but without ASCII formatting.
    Sorted by descending call count to match the table view.
    """
    by_tool: dict[str, dict[str, Any]] = {}
    for e in entries:
        tool = str(e.get("tool", "-"))
        bucket = by_tool.setdefault(tool, {"calls": 0, "ok": 0, "err": 0, "durations": []})
        bucket["calls"] += 1
        if str(e.get("result", "")) == "ok":
            bucket["ok"] += 1
        else:
            bucket["err"] += 1
        dur = e.get("duration_ms")
        if isinstance(dur, int):
            bucket["durations"].append(dur)
    out: list[dict[str, Any]] = []
    for tool, bucket in sorted(by_tool.items(), key=lambda kv: -int(kv[1]["calls"])):
        durations: list[int] = sorted(bucket["durations"])
        out.append(
            {
                "tool": tool,
                "calls": bucket["calls"],
                "ok": bucket["ok"],
                "err": bucket["err"],
                "p50_ms": _percentile(durations, 50),
                "p95_ms": _percentile(durations, 95),
                "max_ms": durations[-1] if durations else 0,
            }
        )
    return out


def _render_stats(entries: list[dict[str, Any]]) -> str:
    """Per-tool summary: count, ok-rate, p50/p95/max latency, total errors.

    Skips entries with no ``duration_ms`` (e.g. failure-path rows that
    were logged before timing was captured). Sorted by descending call
    count so the busiest tools surface first.
    """
    by_tool: dict[str, dict[str, Any]] = {}
    for e in entries:
        tool = str(e.get("tool", "-"))
        bucket = by_tool.setdefault(tool, {"calls": 0, "ok": 0, "err": 0, "durations": []})
        bucket["calls"] += 1
        if str(e.get("result", "")) == "ok":
            bucket["ok"] += 1
        else:
            bucket["err"] += 1
        dur = e.get("duration_ms")
        if isinstance(dur, int):
            bucket["durations"].append(dur)

    if not by_tool:
        return "(no audit entries match the filters)"

    rows: list[tuple[str, ...]] = [("TOOL", "CALLS", "OK", "ERR", "P50ms", "P95ms", "MAXms")]
    ordered = sorted(by_tool.items(), key=lambda kv: -int(kv[1]["calls"]))
    for tool, bucket in ordered:
        durations: list[int] = sorted(bucket["durations"])
        rows.append(
            (
                tool,
                str(bucket["calls"]),
                str(bucket["ok"]),
                str(bucket["err"]),
                str(_percentile(durations, 50)),
                str(_percentile(durations, 95)),
                str(durations[-1] if durations else 0),
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    out: list[str] = []
    for row in rows:
        out.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return "\n".join(out)


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
    parser.add_argument(
        "--stats",
        action="store_true",
        help=(
            "Summarise by tool: call counts, ok/error split, "
            "p50/p95/max latency in ms. Ignores --tail."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit machine-readable JSON instead of the formatted table. "
            "Combine with --stats for CI / dashboard ingestion."
        ),
    )
    ns = parser.parse_args(argv)

    # Performance: when --errors (24h window) or --since N is set, skip
    # rotated audit files older than the window. Loading 30 days of
    # history just to throw it away costs real time on a long-running
    # install. Stats / unfiltered queries still load everything.
    since_minutes: int | None = None
    if ns.since is not None and ns.since >= 0:
        since_minutes = ns.since
    elif ns.errors:
        since_minutes = 24 * 60

    entries = _load_all_entries(since_minutes=since_minutes)
    filtered = _filter(
        entries,
        errors_only=ns.errors,
        instance=ns.instance,
        since_minutes=ns.since,
    )

    if ns.stats:
        # Stats run over every filtered entry — --tail would distort the
        # percentiles by truncating the sample.
        if ns.json:
            print(json.dumps(_stats_payload(filtered), separators=(",", ":")))
        else:
            print(_render_stats(filtered))
        return 0

    # `--tail` applies last, after filters.
    if ns.tail > 0:
        filtered = filtered[-ns.tail :]

    if not filtered:
        if ns.json:
            print("[]")
        else:
            print("(no audit entries match the filters)")
        return 0

    if ns.json:
        print(json.dumps(filtered, separators=(",", ":")))
        return 0

    print(_render_table(filtered))
    return 0


if __name__ == "__main__":
    sys.exit(main())
