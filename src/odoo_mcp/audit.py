"""Append-only JSONL audit log.

Every tool invocation — success or failure — writes exactly one line. The log
is designed to be safe to keep around:

* **Never logs field values, credentials, or domain operands.** Only metadata:
  timestamp, instance, tool, model, operation, result code, record count,
  duration, and the ``dry_run`` flag.
* **Daily rotation** — ``audit.jsonl`` always points at the current day.
  Older files are kept as ``audit-YYYY-MM-DD.jsonl`` and trimmed to the
  retention window on startup.
* **Fail-closed** — if a write raises, :meth:`AuditLog.log` re-raises as
  :class:`AuditLogError` so the dispatcher can refuse the tool call.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

from .errors import AuditLogError

_RETENTION_DAYS = 30
_ROTATED_PATTERN = re.compile(r"audit-(\d{4}-\d{2}-\d{2})\.jsonl$")


@dataclass(slots=True)
class AuditEvent:
    instance: str
    tool: str
    op: str
    model: str | None
    result: str  # "ok" | error code
    record_count: int | None
    duration_ms: int
    dry_run: bool
    details: dict[str, str | int | bool | None]  # ONLY metadata, no user data


class AuditLog:
    def __init__(self, path: Path) -> None:
        self._path = path.expanduser()
        self._lock = Lock()
        self._open()

    # --- Lifecycle ----------------------------------------------------------

    def _open(self) -> None:
        """Ensure the log directory exists and write a startup marker.

        Raises :class:`AuditLogError` if the directory can't be created or
        the file can't be written. This is the fail-closed check — if it
        throws, :mod:`odoo_mcp.server` refuses to start.
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            marker = {
                "ts": _now_iso(),
                "event": "audit_log_open",
                "path": str(self._path),
            }
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(marker, separators=(",", ":")) + "\n")
        except OSError as exc:
            raise AuditLogError(
                f"Cannot write to audit log at {self._path}: {exc}"
            ) from exc
        self._rotate_if_needed()
        self._trim_retention()

    def _rotate_if_needed(self) -> None:
        """Rotate ``audit.jsonl`` to ``audit-YYYY-MM-DD.jsonl`` once per day.

        We detect "stale" by comparing the file's mtime date to today. No
        locking races to worry about — this runs once at startup.
        """
        if not self._path.exists():
            return
        try:
            mtime = datetime.fromtimestamp(self._path.stat().st_mtime, tz=UTC)
        except OSError:
            return
        today = datetime.now(tz=UTC).date()
        if mtime.date() == today:
            return
        rotated = self._path.with_name(f"audit-{mtime.date().isoformat()}.jsonl")
        # If the rotated file already exists (previous rotation failed
        # mid-way), append rather than clobber.
        try:
            if rotated.exists():
                with self._path.open("rb") as src, rotated.open("ab") as dst:
                    dst.write(src.read())
                self._path.unlink()
            else:
                self._path.rename(rotated)
        except OSError as exc:
            raise AuditLogError(
                f"Failed to rotate audit log {self._path} -> {rotated}: {exc}"
            ) from exc

    def _trim_retention(self) -> None:
        """Delete rotated files older than ``_RETENTION_DAYS`` days."""
        cutoff = datetime.now(tz=UTC).date().toordinal() - _RETENTION_DAYS
        try:
            for entry in self._path.parent.iterdir():
                m = _ROTATED_PATTERN.match(entry.name)
                if not m:
                    continue
                try:
                    entry_date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
                except ValueError:
                    continue
                if entry_date.toordinal() < cutoff:
                    try:
                        entry.unlink()
                    except OSError:
                        # Best-effort — don't fail startup on retention cleanup.
                        continue
        except OSError:
            # The directory vanished under us. _open already verified
            # writability; this is best-effort so swallow and move on.
            return

    # --- Writing ------------------------------------------------------------

    def log(self, event: AuditEvent) -> None:
        """Append one event. Raises :class:`AuditLogError` on write failure."""
        payload = {
            "ts": _now_iso(),
            "instance": event.instance,
            "tool": event.tool,
            "op": event.op,
            "model": event.model,
            "result": event.result,
            "record_count": event.record_count,
            "duration_ms": event.duration_ms,
            "dry_run": event.dry_run,
            "details": event.details,
        }
        line = json.dumps(payload, separators=(",", ":"), default=str) + "\n"
        try:
            with self._lock, self._path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
        except OSError as exc:
            raise AuditLogError(
                f"Failed to write audit entry to {self._path}: {exc}"
            ) from exc


def _now_iso() -> str:
    # ISO-8601, second precision, always UTC with the 'Z' suffix.
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
