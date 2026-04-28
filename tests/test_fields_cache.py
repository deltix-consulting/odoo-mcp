"""Tests for the SQLite-backed persistent fields cache."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from odoo_mcp.fields_cache import PersistentFieldsCache


def _payload(n: int = 1) -> dict[str, dict[str, object]]:
    return {f"field_{i}": {"type": "char", "string": f"Field {i}"} for i in range(n)}


def test_get_returns_none_when_empty(tmp_path: Path) -> None:
    cache = PersistentFieldsCache(tmp_path / "fc.db")
    assert cache.get("dev", "res.partner") is None


def test_put_then_get_returns_payload(tmp_path: Path) -> None:
    cache = PersistentFieldsCache(tmp_path / "fc.db")
    payload = _payload(3)
    cache.put("dev", "res.partner", payload)
    got = cache.get("dev", "res.partner")
    assert got == payload


def test_get_returns_none_when_expired(tmp_path: Path) -> None:
    cache = PersistentFieldsCache(tmp_path / "fc.db", ttl_seconds=1)
    cache.put("dev", "res.partner", _payload())
    # Sanity-check fresh hit.
    assert cache.get("dev", "res.partner") is not None
    time.sleep(1.2)
    assert cache.get("dev", "res.partner") is None


def test_put_overwrites_existing_row(tmp_path: Path) -> None:
    cache = PersistentFieldsCache(tmp_path / "fc.db")
    cache.put("dev", "res.partner", _payload(1))
    cache.put("dev", "res.partner", _payload(5))
    got = cache.get("dev", "res.partner")
    assert got is not None
    assert len(got) == 5


def test_invalidate_one_model(tmp_path: Path) -> None:
    cache = PersistentFieldsCache(tmp_path / "fc.db")
    cache.put("dev", "res.partner", _payload())
    cache.put("dev", "crm.lead", _payload())
    cache.invalidate("dev", "res.partner")
    assert cache.get("dev", "res.partner") is None
    assert cache.get("dev", "crm.lead") is not None


def test_invalidate_whole_instance(tmp_path: Path) -> None:
    cache = PersistentFieldsCache(tmp_path / "fc.db")
    cache.put("dev", "res.partner", _payload())
    cache.put("dev", "crm.lead", _payload())
    cache.put("prod", "res.partner", _payload())
    cache.invalidate("dev")
    assert cache.get("dev", "res.partner") is None
    assert cache.get("dev", "crm.lead") is None
    # Other instance untouched.
    assert cache.get("prod", "res.partner") is not None


def test_clear_removes_all(tmp_path: Path) -> None:
    cache = PersistentFieldsCache(tmp_path / "fc.db")
    cache.put("dev", "res.partner", _payload())
    cache.put("prod", "crm.lead", _payload())
    cache.clear()
    assert cache.get("dev", "res.partner") is None
    assert cache.get("prod", "crm.lead") is None
    assert cache.info()["row_count"] == 0


def test_concurrent_writes_dont_corrupt(tmp_path: Path) -> None:
    cache = PersistentFieldsCache(tmp_path / "fc.db")
    models = [f"model.{i}" for i in range(5)]

    def writer(name: str) -> None:
        cache.put("dev", name, _payload(2))

    threads = [threading.Thread(target=writer, args=(m,)) for m in models]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for name in models:
        got = cache.get("dev", name)
        assert got is not None
        assert len(got) == 2


def test_chmod_600_on_create(tmp_path: Path) -> None:
    if os.name != "posix":
        return
    path = tmp_path / "fc.db"
    PersistentFieldsCache(path)
    mode = path.stat().st_mode & 0o777
    assert mode & 0o077 == 0
    # Owner read/write must be set.
    assert mode & 0o600 == 0o600


def test_info_reports_counts_and_timestamps(tmp_path: Path) -> None:
    cache = PersistentFieldsCache(tmp_path / "fc.db")
    info_empty = cache.info()
    assert info_empty["row_count"] == 0
    assert info_empty["oldest_fetched_at"] is None
    assert info_empty["newest_fetched_at"] is None

    cache.put("dev", "res.partner", _payload())
    info = cache.info()
    assert info["row_count"] == 1
    assert isinstance(info["oldest_fetched_at"], float)
    assert isinstance(info["newest_fetched_at"], float)
    assert info["file_size_bytes"] > 0


def test_corrupt_payload_treated_as_miss(tmp_path: Path) -> None:
    """Garbage stored as JSON in the row should fail-soft to a miss."""
    import sqlite3

    cache = PersistentFieldsCache(tmp_path / "fc.db")
    # Inject a row with non-dict JSON.
    with sqlite3.connect(str(tmp_path / "fc.db")) as conn:
        conn.execute(
            "INSERT INTO fields(instance, model, payload, fetched_at) VALUES(?,?,?,?)",
            ("dev", "res.partner", "[1,2,3]", time.time()),
        )
        conn.commit()
    assert cache.get("dev", "res.partner") is None
