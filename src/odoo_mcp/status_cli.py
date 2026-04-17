"""`odoo-mcp status` — runtime visibility command.

Prints a human-friendly report covering the configured instances, the
rate-limiter state, the prod-write unlock state, and the last few audit
entries. Never contacts Odoo (authentication is lazy, and this CLI does
not trigger any tool calls).
"""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime
from typing import Any

from . import __version__
from .audit_cli import _format_detail, _load_all_entries
from .errors import OdooMcpError
from .server import OdooMcpApp, build_app


def _format_relative(seconds: float) -> str:
    """Return a short "Xs" / "Xm Ys" / "Xh Ym" string for *seconds*."""
    seconds = max(0.0, seconds)
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        m, s = divmod(total, 60)
        return f"{m}m {s:02d}s"
    h, rem = divmod(total, 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h {m:02d}m"


def _format_ago(ts_str: str, now: datetime) -> str:
    try:
        ts = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return "?"
    delta = (now - ts).total_seconds()
    return f"{_format_relative(delta)} ago"


def _render(app: OdooMcpApp) -> str:
    lines: list[str] = []
    lines.append("Odoo MCP status")
    lines.append("================")
    lines.append(f"Version:       {__version__}")
    lines.append(f"Config:        {app.config.path}")
    lines.append(f"Audit log:     {app.config.audit_log_path}")
    lines.append("")

    lines.append(f"Instances ({len(app.instances)})")
    lines.append("-------------")

    now_mono = time.monotonic()
    now_utc = datetime.now(tz=UTC)

    # Map instance -> last audit entry for "last call Xs ago".
    all_entries = _load_all_entries()
    last_by_instance: dict[str, dict[str, Any]] = {}
    for e in all_entries:
        inst = str(e.get("instance", ""))
        if inst and inst != "-":
            last_by_instance[inst] = e

    for name, rt in app.instances.items():
        url = rt.config.url
        db = rt.config.database
        env = "production" if rt.config.production else "dev"
        lines.append(f"[{name}] {url} / {db} ({env})")

        # Auth status
        uid = rt.client._uid  # lazy-auth state; None until first tool call
        if uid is None:
            auth_line = "Auth:        not yet attempted"
        else:
            last = last_by_instance.get(name)
            ago = (
                _format_ago(str(last.get("ts", "")), now_utc) if last is not None else "no activity"
            )
            auth_line = f"Auth:        \u2713 uid={uid}  (last call {ago})"
        lines.append(f"  {auth_line}")

        # Rate limit
        try:
            tokens = app.rate_limiter.peek(name, now=now_mono)
            capacity = app.rate_limiter.capacity(name)
        except OdooMcpError:
            lines.append("  Rate limit:  (unconfigured)")
        else:
            lines.append(f"  Rate limit:  {int(capacity)} / min   ({tokens:.1f} tokens available)")

        # Writes
        if not rt.config.production:
            lines.append("  Writes:      unlocked (non-production instance)")
        elif app.prod_guard.is_unlocked(name, now=now_mono):
            # Peek at expiry without mutating state.
            expiry = app.prod_guard._unlocked_until.get(name)
            if expiry is not None:
                remain = _format_relative(expiry - now_mono)
                lines.append(f"  Writes:      unlocked (auto-lock in {remain})")
            else:
                lines.append("  Writes:      unlocked")
        else:
            lines.append("  Writes:      LOCKED")

        lines.append("")

    lines.append("Recent activity (last 5 audit entries)")
    lines.append("--------------------------------------")
    recent = all_entries[-5:]
    if not recent:
        lines.append("(no audit entries yet)")
    else:
        # Compute dynamic column widths so long values (e.g. "model_not_allowed")
        # don't break the alignment.
        rows: list[tuple[str, str, str, str, str, str]] = []
        for e in recent:
            rows.append(
                (
                    str(e.get("ts", "")),
                    str(e.get("result", "")),
                    str(e.get("tool", "")),
                    str(e.get("instance", "-")),
                    str(e.get("model") or "-"),
                    _format_detail(e),
                )
            )
        widths = [0, 0, 0, 0, 0]
        for row in rows:
            for i in range(5):  # all but the last (free-form detail)
                widths[i] = max(widths[i], len(row[i]))
        for ts, result, tool, inst, model, detail in rows:
            lines.append(
                f"{ts:<{widths[0]}}  {result:<{widths[1]}}  "
                f"{tool:<{widths[2]}}  {inst:<{widths[3]}}  "
                f"{model:<{widths[4]}}  {detail}".rstrip()
            )

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    _ = argv  # no flags currently
    try:
        app = build_app()
    except OdooMcpError as exc:
        print(f"Cannot build status: {exc.user_message}", file=sys.stderr)
        return 1
    print(_render(app), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
