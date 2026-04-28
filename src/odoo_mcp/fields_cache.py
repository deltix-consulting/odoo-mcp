"""Persistent ``fields_get`` cache backed by SQLite.

In-memory caching on :class:`odoo_mcp.client.OdooClient` already eliminates
duplicate ``fields_get`` round-trips within one process. This module adds a
second level: a SQLite-backed cache that survives process restarts, so the
common case of "Claude restarts and re-asks for ``res.partner`` fields" no
longer pays the round-trip tax every time.

Design notes
------------

* **Stdlib only.** ``sqlite3`` is part of CPython, so no new dependency.
* **Tiny schema.** One table, primary key ``(instance, model)``, payload
  serialized as JSON. ``fields_get`` returns plain dicts of strings and
  primitives — JSON round-trips losslessly.
* **TTL is per-entry.** A read compares ``time.time() - fetched_at`` against
  the configured TTL and treats anything stale as a miss.
* **No secrets, ever.** ``fields_get`` is metadata only — field types, labels,
  help text. No record values pass through this code path.
* **Owner-only file mode.** chmod 0o600 on creation, mirroring the audit log
  and config-file posture.
* **Thread-safe.** A single :class:`threading.Lock` serializes writes; reads
  pull a fresh connection per call (sqlite handles concurrent readers fine).

The cache is opt-out at the config layer: an empty ``fields_cache_path``
string disables the L2 entirely and the client falls back to its in-memory
dict only.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fields (
    instance TEXT NOT NULL,
    model TEXT NOT NULL,
    payload TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    PRIMARY KEY (instance, model)
)
"""


class PersistentFieldsCache:
    """SQLite-backed L2 cache for ``fields_get`` payloads.

    Construction creates (or opens) the DB file, sets ``chmod 0o600`` if we
    just created it, and ensures the schema exists. All public methods are
    safe to call from multiple threads.
    """

    def __init__(self, path: Path, ttl_seconds: int = 86400) -> None:
        self._path = Path(path).expanduser()
        self._ttl = int(ttl_seconds)
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        existed = self._path.exists()
        with self._connect() as conn:
            conn.execute(_SCHEMA)
            conn.commit()
        if not existed:
            self._chmod_owner_only()

    # --- file-mode hardening ------------------------------------------------

    def _chmod_owner_only(self) -> None:
        """Restrict the cache file to owner read/write.

        The cache only holds metadata, not field values, but we still apply
        the same posture as the audit log and config file — nothing in
        ``~/.odoo-mcp/`` should be group/world readable. On non-POSIX
        platforms (Windows) ``chmod`` is a no-op.
        """
        if os.name != "posix":
            return
        try:
            os.chmod(self._path, 0o600)
        except OSError as exc:
            logger.warning("Could not chmod 600 fields cache %s: %s", self._path, exc)

    # --- connections --------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        # ``check_same_thread=False`` is safe because we serialize writes
        # with our own lock and reads use short-lived connections.
        return sqlite3.connect(str(self._path), check_same_thread=False)

    # --- public API ---------------------------------------------------------

    def get(self, instance: str, model: str) -> dict[str, dict[str, Any]] | None:
        """Return the cached payload for ``(instance, model)`` or ``None``.

        ``None`` if there is no row, or if the row is older than the
        configured TTL. Corrupt JSON or unexpected payload shapes are also
        treated as a miss — the caller will re-fetch from Odoo and
        overwrite via :meth:`put`.
        """
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT payload, fetched_at FROM fields WHERE instance = ? AND model = ?",
                    (instance, model),
                ).fetchone()
        except sqlite3.Error as exc:
            logger.warning("fields cache read failed (%s, %s): %s", instance, model, exc)
            return None
        if row is None:
            return None
        payload_text, fetched_at = row
        if not isinstance(fetched_at, (int, float)):
            return None
        if (time.time() - float(fetched_at)) > self._ttl:
            return None
        try:
            decoded = json.loads(payload_text)
        except (TypeError, ValueError):
            return None
        if not isinstance(decoded, dict):
            return None
        # Shallow type check — values must be dicts. Don't validate further;
        # the caller treats this exactly like a fresh ``fields_get`` result.
        for value in decoded.values():
            if not isinstance(value, dict):
                return None
        return decoded

    def put(self, instance: str, model: str, payload: dict[str, dict[str, Any]]) -> None:
        """UPSERT ``payload`` for ``(instance, model)``."""
        try:
            encoded = json.dumps(payload, separators=(",", ":"), default=str)
        except (TypeError, ValueError) as exc:
            logger.warning("fields cache: cannot encode %s/%s: %s", instance, model, exc)
            return
        now = time.time()
        with self._lock:
            try:
                with self._connect() as conn:
                    conn.execute(
                        "INSERT INTO fields(instance, model, payload, fetched_at) "
                        "VALUES(?,?,?,?) "
                        "ON CONFLICT(instance, model) DO UPDATE SET "
                        "payload = excluded.payload, fetched_at = excluded.fetched_at",
                        (instance, model, encoded, now),
                    )
                    conn.commit()
            except sqlite3.Error as exc:
                logger.warning("fields cache write failed (%s, %s): %s", instance, model, exc)

    def invalidate(self, instance: str, model: str | None = None) -> None:
        """Drop one model's row, or every row for ``instance`` if ``model is None``."""
        with self._lock:
            try:
                with self._connect() as conn:
                    if model is None:
                        conn.execute("DELETE FROM fields WHERE instance = ?", (instance,))
                    else:
                        conn.execute(
                            "DELETE FROM fields WHERE instance = ? AND model = ?",
                            (instance, model),
                        )
                    conn.commit()
            except sqlite3.Error as exc:
                logger.warning("fields cache invalidate failed: %s", exc)

    def clear(self) -> None:
        """Delete every row. Used by ``odoo-mcp cache --clear``."""
        with self._lock:
            try:
                with self._connect() as conn:
                    conn.execute("DELETE FROM fields")
                    conn.commit()
            except sqlite3.Error as exc:
                logger.warning("fields cache clear failed: %s", exc)

    # --- introspection (used by `odoo-mcp cache --info`) -------------------

    def info(self) -> dict[str, Any]:
        """Return a small dict summarizing cache state. Never raises."""
        try:
            size = self._path.stat().st_size if self._path.exists() else 0
        except OSError:
            size = 0
        row_count = 0
        oldest: float | None = None
        newest: float | None = None
        try:
            with self._connect() as conn:
                cur = conn.execute("SELECT COUNT(*), MIN(fetched_at), MAX(fetched_at) FROM fields")
                row = cur.fetchone()
                if row is not None:
                    rc, mn, mx = row
                    row_count = int(rc) if rc is not None else 0
                    oldest = float(mn) if isinstance(mn, (int, float)) else None
                    newest = float(mx) if isinstance(mx, (int, float)) else None
        except sqlite3.Error as exc:
            logger.warning("fields cache info failed: %s", exc)
        return {
            "path": str(self._path),
            "file_size_bytes": size,
            "row_count": row_count,
            "oldest_fetched_at": oldest,
            "newest_fetched_at": newest,
            "ttl_seconds": self._ttl,
        }

    @property
    def path(self) -> Path:
        return self._path

    @property
    def ttl_seconds(self) -> int:
        return self._ttl
