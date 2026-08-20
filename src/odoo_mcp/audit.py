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
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from threading import Lock

from .errors import AuditLogError

logger = logging.getLogger(__name__)

_RETENTION_DAYS = 30
# Owner read/write only. The audit log records operational metadata
# (instance names, models touched, tool names, timestamps, counts) that
# should not be readable by other local users on a shared machine —
# the same posture the config file and the fields cache already enforce.
_OWNER_ONLY_MODE = 0o600
_ROTATED_PATTERN = re.compile(r"audit-(\d{4}-\d{2}-\d{2})\.jsonl$")

# Audit detail leaves: only primitives or lists of primitives. NO arbitrary
# objects, NO nested-nested dicts — exactly one level of nesting is allowed
# (the top-level dict can contain a dict value, but those inner dicts must
# bottom out at primitives). This is enforced by the dispatcher sanitizer;
# the type alias here is the documented contract.
AuditLeaf = str | int | bool | None | list[str]
AuditDetails = dict[str, AuditLeaf | dict[str, AuditLeaf]]


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
    details: AuditDetails  # ONLY metadata + shape info, never field values


class AuditLog:
    def __init__(self, path: Path) -> None:
        self._path = path.expanduser()
        self._lock = Lock()
        # Tracks the date we last verified the log was current (UTC). A
        # long-running process that crosses midnight would otherwise keep
        # writing into yesterday's file because ``_rotate_if_needed`` only
        # ran at startup. ``log()`` re-checks via this field cheaply.
        self._last_rotation_check: date | None = None
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
            raise AuditLogError(f"Cannot write to audit log at {self._path}: {exc}") from exc
        # Lock the file down to owner-only BEFORE anything else. Files
        # created with the default umask land at 0o644 (world-readable);
        # re-chmod here also remediates logs from installs that predate
        # this hardening.
        _chmod_owner_only(self._path)
        self._rotate_if_needed()
        self._trim_retention()
        self._last_rotation_check = datetime.now(tz=UTC).date()

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
        # The dated file inherits the rotated content; keep it owner-only.
        _chmod_owner_only(rotated)

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
                else:
                    # Kept within the retention window — remediate the
                    # file mode in case it was created world-readable by
                    # a version that predated the owner-only hardening.
                    _chmod_owner_only(entry)
        except OSError:
            # The directory vanished under us. _open already verified
            # writability; this is best-effort so swallow and move on.
            return

    # --- Writing ------------------------------------------------------------

    def preflight(self) -> None:
        """Verify the log is writable, without emitting a record.

        Called on the write path BEFORE the Odoo mutation. The audit log is
        documented as fail-closed, but logging a successful write happens
        *after* the write has already committed — so a broken log at that
        point cannot prevent the unaudited side effect, it can only report
        it (and, worse, report it as a failure, inviting the agent to retry
        and mutate twice). Checking writability up front is what actually
        delivers the property: if the log is unwritable, the tool call is
        refused before anything changes in Odoo.

        Raises :class:`AuditLogError` if the log cannot be appended to.
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8"):
                pass
        except OSError as exc:
            raise AuditLogError(
                f"Audit log at {self._path} is not writable ({exc}) — refusing the "
                f"write before it reaches Odoo, so no unaudited change can occur."
            ) from exc

    def log(self, event: AuditEvent) -> None:
        """Append one event. Raises :class:`AuditLogError` on write failure."""
        # Cheap mid-flight rotation check: if the date has rolled over since
        # the last write, rotate yesterday's content into a dated file before
        # appending. We compare against ``_last_rotation_check`` first so the
        # hot path is one date comparison; only on a date change do we stat
        # the file and (rarely) rename. This keeps long-running MCPs from
        # writing Tuesday's events into Monday's ``audit.jsonl``.
        today = datetime.now(tz=UTC).date()
        rotated_this_call = False
        if self._last_rotation_check != today:
            self._rotate_if_needed()
            self._last_rotation_check = today
            rotated_this_call = True
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
            logger.error("audit log write failed: %s: %s", self._path, exc)
            raise AuditLogError(f"Failed to write audit entry to {self._path}: {exc}") from exc
        # A mid-flight rotation just renamed the old file away; the append
        # above created a fresh audit.jsonl under the default umask. Lock
        # it back down. Only runs on the once-per-day rollover, not the
        # hot path.
        if rotated_this_call:
            _chmod_owner_only(self._path)


def _chmod_owner_only(path: Path) -> None:
    """Restrict ``path`` to owner read/write (0o600), best-effort.

    No-op on non-POSIX platforms (Windows), where ``st_mode`` bits do
    not carry the same meaning. A chmod failure is logged at WARNING and
    swallowed: it must not take down audit logging, which is fail-closed
    on *write* failures but not on a hardening step. Mirrors the same
    posture as :class:`odoo_mcp.fields_cache.PersistentFieldsCache`.
    """
    if os.name != "posix":
        return
    try:
        os.chmod(path, _OWNER_ONLY_MODE)
    except OSError as exc:
        logger.warning("Could not chmod 600 audit log %s: %s", path, exc)


def _now_iso() -> str:
    # ISO-8601, second precision, always UTC with the 'Z' suffix.
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
