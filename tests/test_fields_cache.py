"""Tests for the SQLite-backed persistent fields cache."""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from odoo_mcp.fields_cache import CACHE_UNAVAILABLE_ERRORS, PersistentFieldsCache


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


def test_pre_v0_14_2_payload_treated_as_miss(tmp_path: Path) -> None:
    """Bare-dict (unwrapped) rows from before the schema version bump miss.

    Pre-v0.14.2 cache rows were stored as ``{...fields...}`` with no
    schema marker. After the bump, those rows should read as a miss so
    the next access re-fetches with the current attribute set (notably
    ``store``).
    """
    import json
    import sqlite3

    cache = PersistentFieldsCache(tmp_path / "fc.db")
    legacy = json.dumps({"id": {"type": "integer"}, "name": {"type": "char"}})
    with sqlite3.connect(str(tmp_path / "fc.db")) as conn:
        conn.execute(
            "INSERT INTO fields(instance, model, payload, fetched_at) VALUES(?,?,?,?)",
            ("dev", "res.partner", legacy, time.time()),
        )
        conn.commit()
    assert cache.get("dev", "res.partner") is None


def test_wrong_schema_version_treated_as_miss(tmp_path: Path) -> None:
    """Wrapped row with an outdated ``_v`` reads as a miss."""
    import json
    import sqlite3

    cache = PersistentFieldsCache(tmp_path / "fc.db")
    stale = json.dumps({"_v": 1, "fields": {"id": {"type": "integer"}}})
    with sqlite3.connect(str(tmp_path / "fc.db")) as conn:
        conn.execute(
            "INSERT INTO fields(instance, model, payload, fetched_at) VALUES(?,?,?,?)",
            ("dev", "res.partner", stale, time.time()),
        )
        conn.commit()
    assert cache.get("dev", "res.partner") is None


def test_round_trip_uses_current_schema_version(tmp_path: Path) -> None:
    """``put`` writes the wrapper; ``get`` decodes it transparently."""
    cache = PersistentFieldsCache(tmp_path / "fc.db")
    payload = {"id": {"type": "integer"}, "name": {"type": "char"}}
    cache.put("dev", "res.partner", payload)
    out = cache.get("dev", "res.partner")
    assert out == payload


# --- unusable cache FILE (as opposed to an unusable row) ------------------
#
# ``test_corrupt_payload_treated_as_miss`` covers a bad row inside a healthy
# DB. These cover the other corruption: a cache file the filesystem hands us
# in a shape SQLite cannot open at all.


def _unusable_paths(tmp_path: Path) -> list[tuple[str, Path]]:
    truncated = tmp_path / "truncated.db"
    truncated.write_bytes(b"SQLite format 3\x00 ...cut off mid-write by a crash")

    unreadable = tmp_path / "unreadable.db"
    unreadable.write_bytes(b"")
    unreadable.chmod(0o000)

    a_directory = tmp_path / "a-directory.db"
    a_directory.mkdir()

    parent_is_a_file = tmp_path / "notadir"
    parent_is_a_file.write_text("x")

    return [
        ("truncated", truncated),
        ("unreadable", unreadable),
        ("a_directory", a_directory),
        ("parent_is_a_file", parent_is_a_file / "sub" / "fc.db"),
    ]


def test_constructor_still_raises_on_an_unusable_file(tmp_path: Path) -> None:
    """The strict constructor keeps its contract — ``open`` is the soft variant."""
    for label, path in _unusable_paths(tmp_path):
        try:
            PersistentFieldsCache(path)
        except CACHE_UNAVAILABLE_ERRORS:
            continue
        raise AssertionError(f"{label}: constructor unexpectedly succeeded")


def test_open_returns_none_on_an_unusable_file(tmp_path: Path) -> None:
    """Every shape the constructor rejects degrades to ``None``, not an exception."""
    for label, path in _unusable_paths(tmp_path):
        assert PersistentFieldsCache.open(path) is None, label


def test_open_warns_and_names_the_path(tmp_path: Path, caplog) -> None:
    bad = tmp_path / "truncated.db"
    bad.write_bytes(b"not a database")
    with caplog.at_level(logging.WARNING, logger="odoo_mcp.fields_cache"):
        assert PersistentFieldsCache.open(bad) is None
    assert str(bad) in caplog.text
    # The operator needs the remedy, not just the symptom.
    assert "delete the file" in caplog.text.lower()


def test_open_returns_a_working_cache_on_a_healthy_file(tmp_path: Path) -> None:
    cache = PersistentFieldsCache.open(tmp_path / "fc.db")
    assert cache is not None
    cache.put("dev", "res.partner", _payload())
    assert cache.get("dev", "res.partner") == _payload()
